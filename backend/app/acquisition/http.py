"""Polite, auditable HTTP client used by every data provider.

Rules this client enforces (agents and providers never fetch on their own):
  * Identifies itself honestly (descriptive User-Agent); never spoofs a browser.
  * Obeys robots.txt (RFC 9309 semantics) and per-host minimum request spacing.
  * Retries only transient failures (timeouts, network errors, 5xx, 429, invalid content) with
    exponential backoff, honouring Retry-After. A 401/403/418 is final: ACCESS_DENIED, no bypass.
  * Validates content: some sites answer HTTP 200 with an error page when rate-limited.
  * Caches successful responses on disk with a per-call TTL, so identical documents are not
    downloaded repeatedly.
  * Never raises for expected failures; returns a FetchResult whose status explains what happened.
"""

import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import robotparser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.models import FetchStatus

Validator = Callable[[bytes, str | None], str | None]
"""Returns an error message if the body is not what the caller expected, else None."""

_RETRYABLE_HTTP = {429, 500, 502, 503, 504}
_DENIED_HTTP = {401, 403, 418}
ROBOTS_OK_TTL = timedelta(hours=24)
ROBOTS_FAIL_TTL = timedelta(minutes=5)


@dataclass
class FetchResult:
    url: str
    status: FetchStatus
    http_status: int | None = None
    content: bytes | None = None
    content_type: str | None = None
    from_cache: bool = False
    attempts: int = 0
    error: str | None = None
    retrieved_at: datetime | None = None
    duration_ms: int = 0
    retry_after_s: float | None = None  # server-requested wait (429/503), if any
    final_url: str | None = None  # the address actually served, after any redirects

    @property
    def ok(self) -> bool:
        return self.status in (FetchStatus.OK, FetchStatus.CACHED)

    @property
    def text(self) -> str:
        return (self.content or b"").decode("utf-8-sig", errors="replace")


@dataclass(frozen=True)
class ClientConfig:
    user_agent: str
    cache_dir: Path | None = None
    min_interval_s: float = 1.0
    host_intervals: dict[str, float] | None = None  # per-host override of min_interval_s
    # Some hosts signal throttling with an unusual status (BSE answers 406 under bursts). Those
    # codes are treated like 429: retried, with the host's own (longer) backoff.
    host_throttle_codes: dict[str, frozenset[int]] | None = None
    host_backoff_s: dict[str, float] | None = None
    timeout_s: float = 30.0
    max_attempts: int = 3
    backoff_base_s: float = 1.0
    max_retry_after_s: float = 60.0
    max_bytes: int = 60 * 1024 * 1024
    respect_robots: bool = True


def redact_query(url: str, params: tuple[str, ...]) -> str:
    """Replace the values of the named query parameters with REDACTED."""
    parts = urlsplit(url)
    wanted = {p.lower() for p in params}
    pairs = [
        (k, "REDACTED" if k.lower() in wanted else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(pairs)))


class PoliteClient:
    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.25),
        recorder: Callable[[str, FetchResult], None] | None = None,
    ) -> None:
        self.config = config
        self._http = httpx.Client(
            transport=transport,
            timeout=config.timeout_s,
            follow_redirects=True,
            headers={"User-Agent": config.user_agent},
        )
        self._sleep, self._clock, self._now, self._jitter = sleep, clock, now, jitter
        self._recorder = recorder
        self._lock = threading.Lock()
        self._next_allowed: dict[str, float] = {}
        self._robots: dict[str, tuple[datetime, robotparser.RobotFileParser | None]] = {}
        self._unreachable: set[str] = set()
        self._cache_dir = config.cache_dir
        if self._cache_dir:
            try:
                self._cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                # No writable cache: carry on without one rather than failing every request.
                logging.getLogger("equityos.acquisition").warning(
                    "cache directory %s is not writable; caching disabled", self._cache_dir
                )
                self._cache_dir = None

    def close(self) -> None:
        self._http.close()

    # ---- public API ------------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        source: str,
        ttl: timedelta | None = None,
        validate: Validator | None = None,
        max_bytes: int | None = None,
        headers: dict[str, str] | None = None,
        documented_api: bool = False,
        secret_params: tuple[str, ...] = (),
    ) -> FetchResult:
        """Fetch `url`. `source` is the source-registry code used in the audit log."""
        started = self._clock()
        result = self._fetch(
            url, ttl, validate, max_bytes or self.config.max_bytes, headers, documented_api
        )
        result.duration_ms = int((self._clock() - started) * 1000)
        if secret_params:
            # Credentials passed as query parameters go on the wire but never into the audit log,
            # stored source URLs or any caller-visible URL.
            result.url = redact_query(url, secret_params)
        if self._recorder:
            self._recorder(source, result)
        return result

    # ---- internals -------------------------------------------------------------------------

    def _fetch(
        self,
        url: str,
        ttl: timedelta | None,
        validate: Validator | None,
        max_bytes: int,
        headers: dict[str, str] | None,
        documented_api: bool,
    ) -> FetchResult:
        # `headers` (e.g. an Authorization token) are sent on the wire only. They are never part of
        # the cache key, the audit log or any FetchResult. `documented_api` marks a call to an
        # authenticated, contractually documented API endpoint: robots.txt is a crawler policy and
        # does not govern those, but every other protection (401/403 etc.) is still final.
        cached = self._cache_read(url, ttl) if ttl else None
        if cached is not None and (
            validate is None or validate(cached.content or b"", cached.content_type) is None
        ):
            return cached

        if self.config.respect_robots and not documented_api and not self._allowed_by_robots(url):
            origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
            if origin in self._unreachable:
                # the host did not answer at all (no DNS record, refused, timed out): not a policy
                return FetchResult(
                    url, FetchStatus.NETWORK_ERROR, error="host unreachable (robots.txt not read)"
                )
            return FetchResult(url, FetchStatus.BLOCKED_BY_ROBOTS, error="Disallowed by robots.txt")

        last = FetchResult(url, FetchStatus.NETWORK_ERROR, error="not attempted")
        for attempt in range(1, self.config.max_attempts + 1):
            self._wait_turn(url)
            last = self._attempt(url, validate, max_bytes, headers)
            last.attempts = attempt
            retryable = last.status in (
                FetchStatus.TIMEOUT,
                FetchStatus.NETWORK_ERROR,
                FetchStatus.SERVER_ERROR,
                FetchStatus.RATE_LIMITED,
                FetchStatus.INVALID_CONTENT,
            )
            if not retryable or attempt == self.config.max_attempts:
                break
            self._sleep(self._backoff(attempt, last))
        if last.status is FetchStatus.OK and ttl:
            self._cache_write(url, last)
        return last

    def _attempt(
        self, url: str, validate: Validator | None, max_bytes: int, headers: dict[str, str] | None
    ) -> FetchResult:
        try:
            with self._http.stream("GET", url, headers=headers) as response:
                code = response.status_code
                ctype = response.headers.get("content-type")
                if code in _DENIED_HTTP:
                    return FetchResult(
                        url,
                        FetchStatus.ACCESS_DENIED,
                        code,
                        self._error_body(response),
                        ctype,
                        error=f"HTTP {code}: access denied (not bypassed)",
                    )
                if code == 404:
                    return FetchResult(
                        url,
                        FetchStatus.NOT_FOUND,
                        code,
                        self._error_body(response),
                        ctype,
                        error="HTTP 404",
                    )
                throttle = (self.config.host_throttle_codes or {}).get(urlsplit(url).netloc, ())
                if code in _RETRYABLE_HTTP or code in throttle:
                    throttled = code == 429 or code in throttle
                    status = FetchStatus.RATE_LIMITED if throttled else FetchStatus.SERVER_ERROR
                    return FetchResult(
                        url,
                        status,
                        code,
                        content_type=ctype,
                        error=f"HTTP {code}",
                        retry_after_s=self._parse_retry_after(response),
                    )
                if code >= 400:  # any other 4xx: our request was refused; retrying will not help
                    return FetchResult(
                        url,
                        FetchStatus.CLIENT_ERROR,
                        code,
                        self._error_body(response),
                        ctype,
                        error=f"HTTP {code}",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        return FetchResult(
                            url,
                            FetchStatus.TOO_LARGE,
                            code,
                            content_type=ctype,
                            error=f"Response exceeds {max_bytes} bytes",
                        )
        except httpx.TimeoutException:
            return FetchResult(url, FetchStatus.TIMEOUT, error="Request timed out")
        except httpx.HTTPError as exc:
            return FetchResult(url, FetchStatus.NETWORK_ERROR, error=type(exc).__name__)

        content = bytes(body)
        problem = validate(content, ctype) if validate else None
        if problem:
            return FetchResult(
                url,
                FetchStatus.INVALID_CONTENT,
                code,
                content,
                ctype,
                error=problem,
                retrieved_at=self._now(),
            )
        return FetchResult(
            url,
            FetchStatus.OK,
            code,
            content,
            ctype,
            retrieved_at=self._now(),
            final_url=str(response.url),
        )

    @staticmethod
    def _error_body(response: httpx.Response) -> bytes:
        """A capped copy of an error response body, so callers can read API error codes."""
        try:
            return response.read()[:65536]
        except httpx.HTTPError:
            return b""

    # ---- pacing, backoff -------------------------------------------------------------------

    def _interval_for(self, host: str) -> float:
        overrides = self.config.host_intervals or {}
        return overrides.get(host, self.config.min_interval_s)

    def _wait_turn(self, url: str) -> None:
        host = urlsplit(url).netloc
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_allowed.get(host, 0.0) - now)
            self._next_allowed[host] = now + wait + self._interval_for(host)
        if wait > 0:
            self._sleep(wait)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value and value.strip().isdigit():
            return float(value.strip())
        return None

    def _backoff(self, attempt: int, last: FetchResult) -> float:
        base = (self.config.host_backoff_s or {}).get(
            urlsplit(last.url).netloc, self.config.backoff_base_s
        )
        delay = base * (2 ** (attempt - 1)) + self._jitter()
        if last.retry_after_s is not None:
            delay = max(delay, last.retry_after_s)
        return min(delay, self.config.max_retry_after_s)

    # ---- robots.txt (RFC 9309) -------------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        now = self._now()
        entry = self._robots.get(origin)
        if entry is None or entry[0] < now:
            parser, ttl = self._load_robots(origin)
            entry = (now + ttl, parser)
            self._robots[origin] = entry
        parser = entry[1]
        if parser is None:  # robots.txt could not be read because of a server error: stay out
            return False
        return parser.can_fetch(self.config.user_agent, url)

    def _load_robots(self, origin: str) -> tuple[robotparser.RobotFileParser | None, timedelta]:
        robots_url = f"{origin}/robots.txt"
        self._wait_turn(robots_url)
        try:
            response = self._http.get(robots_url)
        except httpx.HTTPError:
            self._unreachable.add(origin)
            return None, ROBOTS_FAIL_TTL  # network failure: be conservative
        self._unreachable.discard(origin)
        code = response.status_code
        parser = robotparser.RobotFileParser()
        if 500 <= code < 600:
            return None, ROBOTS_FAIL_TTL
        if code >= 400:
            parser.parse([])  # 4xx: "unavailable" means no restrictions (RFC 9309 s2.3.1.3)
            return parser, ROBOTS_OK_TTL
        ctype = (response.headers.get("content-type") or "").lower()
        if "text/plain" not in ctype:
            # Many sites answer /robots.txt with their HTML app shell. That is not a policy file.
            parser.parse([])
            return parser, ROBOTS_OK_TTL
        parser.parse(response.text.splitlines())
        return parser, ROBOTS_OK_TTL

    # ---- disk cache ------------------------------------------------------------------------

    def _cache_paths(self, url: str) -> tuple[Path, Path] | None:
        if not self._cache_dir:
            return None
        key = hashlib.sha256(url.encode()).hexdigest()
        base = self._cache_dir / key[:2]
        return base / f"{key}.body", base / f"{key}.json"

    def _cache_read(self, url: str, ttl: timedelta) -> FetchResult | None:
        paths = self._cache_paths(url)
        if not paths or not paths[0].exists() or not paths[1].exists():
            return None
        try:
            meta = json.loads(paths[1].read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(meta["retrieved_at"])
            if self._now() - retrieved > ttl:
                return None
            return FetchResult(
                url,
                FetchStatus.CACHED,
                meta.get("http_status"),
                paths[0].read_bytes(),
                meta.get("content_type"),
                from_cache=True,
                attempts=0,
                retrieved_at=retrieved,
            )
        except (OSError, ValueError, KeyError):
            return None  # a corrupt cache entry is just a miss

    def _cache_write(self, url: str, result: FetchResult) -> None:
        paths = self._cache_paths(url)
        if not paths or result.content is None or result.retrieved_at is None:
            return
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[0].write_bytes(result.content)
        paths[1].write_text(
            json.dumps(
                {
                    "retrieved_at": result.retrieved_at.isoformat(),
                    "http_status": result.http_status,
                    "content_type": result.content_type,
                }
            ),
            encoding="utf-8",
        )
