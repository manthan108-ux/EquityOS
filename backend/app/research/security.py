"""Access control for the research endpoints: who is calling, what they may see, how often.

* AUTHENTICATION: an API key in the `X-API-Key` header. Clients are configured in the environment
  (`RESEARCH_API_KEYS="alice:secret1,ops:secret2:admin"`). Only the SHA-256 digest of a key is kept
  in memory and keys are compared in constant time; a key is never logged or put in an error.
  With no keys configured the research endpoints refuse every request (secure by default).
* AUTHORIZATION: a research session, its uploads and its reports belong to the client that created
  them. Another client gets "not found" (not "forbidden", so existence is not revealed). The
  `admin` role may see everything. The company-level vault shows other users only what was
  validated and declared a public filing, and only its metadata.
* RATE LIMITS: a sliding window per client and bucket, so a stuck script cannot flood uploads or
  report generation. (In-memory: per process. A shared limiter is needed once several workers run.)
"""

import hashlib
import hmac
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from app.core.config import get_settings


@dataclass(frozen=True)
class Principal:
    name: str
    is_admin: bool = False


def _digest(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()


def configured_clients() -> list[tuple[str, bytes, bool]]:
    clients = []
    for part in (get_settings().research_api_keys or "").split(","):
        bits = [b.strip() for b in part.strip().split(":")]
        if len(bits) >= 2 and bits[0] and bits[1]:
            clients.append((bits[0], _digest(bits[1]), len(bits) > 2 and bits[2] == "admin"))
    return clients


def current_principal(x_api_key: str | None = Header(default=None)) -> Principal:
    clients = configured_clients()
    if not clients:
        raise HTTPException(
            status_code=503,
            detail="research access is not configured: set RESEARCH_API_KEYS on the server",
        )
    if not x_api_key:
        raise HTTPException(status_code=401, detail="an API key is required (X-API-Key header)")
    given = _digest(x_api_key)
    match = None
    for name, digest, admin in clients:  # no early exit: constant work per request
        if hmac.compare_digest(digest, given):
            match = Principal(name, admin)
    if match is None:
        raise HTTPException(status_code=401, detail="the API key is not valid")
    return match


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: tuple[str, str], limit: int, window: float) -> float | None:
        """None if allowed; otherwise the seconds until a request would be."""
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return window - (now - hits[0])
            hits.append(now)
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limited(bucket: str, per_hour: bool = False):
    """Dependency factory: limit a client's calls to a bucket."""

    def dependency(
        request: Request, principal: Annotated[Principal, Depends(current_principal)]
    ) -> Principal:
        settings = get_settings()
        limit = (
            settings.research_upload_rate_per_hour
            if per_hour
            else settings.research_rate_limit_per_minute
        )
        wait = limiter.check((principal.name, bucket), limit, 3600.0 if per_hour else 60.0)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail="too many requests; slow down",
                headers={"Retry-After": str(int(wait) + 1)},
            )
        return principal

    return dependency
