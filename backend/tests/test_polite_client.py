"""PoliteClient behaviour, tested with a mock transport and a fake clock (no network, no sleeping)."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.acquisition.http import ClientConfig, PoliteClient
from app.models import FetchStatus

UA = "EquityOS-test/1.0 (+https://example.test)"
URL = "https://data.example.test/file.csv"


class Env:
    """A fake world: scripted responses, a fake clock advanced by sleep(), request log."""

    def __init__(self, handler, **config):
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []
        self.t = 1000.0
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)
        self.recorded: list[tuple[str, object]] = []

        def transport_handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        defaults = {"user_agent": UA, "min_interval_s": 1.0, "respect_robots": False}
        self.client = PoliteClient(
            ClientConfig(**{**defaults, **config}),
            transport=httpx.MockTransport(transport_handler),
            sleep=self._sleep,
            clock=lambda: self.t,
            now=lambda: self.wall,
            jitter=lambda: 0.0,
            recorder=lambda source, result: self.recorded.append((source, result)),
        )

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        self.wall += timedelta(seconds=seconds)

    def fetch(self, url=URL, **kw):
        return self.client.fetch(url, source="TEST", **kw)

    def hits(self, path: str) -> int:
        return sum(1 for r in self.requests if r.url.path == path)


def ok(body=b"hello", ctype="text/csv"):
    return lambda request: httpx.Response(200, content=body, headers={"content-type": ctype})


def test_success_and_honest_user_agent():
    env = Env(ok())
    result = env.fetch()
    assert result.status is FetchStatus.OK and result.ok
    assert result.content == b"hello" and result.attempts == 1
    assert env.requests[0].headers["user-agent"] == UA  # never spoofs a browser


def test_requests_to_the_same_host_are_spaced():
    env = Env(ok(), min_interval_s=5.0)
    env.fetch(URL)
    env.fetch("https://data.example.test/other.csv")
    assert env.sleeps == [5.0]


def test_different_hosts_do_not_wait_for_each_other():
    env = Env(ok(), min_interval_s=5.0)
    env.fetch(URL)
    env.fetch("https://another.example.test/x")
    assert env.sleeps == []


def test_per_host_interval_override():
    env = Env(ok(), min_interval_s=1.0, host_intervals={"data.example.test": 9.0})
    env.fetch(URL)
    env.fetch(URL + "?x=1")
    assert env.sleeps == [9.0]


def test_transient_server_errors_are_retried_with_exponential_backoff():
    responses = iter([503, 503, 200])

    def handler(request):
        code = next(responses)
        return httpx.Response(code, content=b"data" if code == 200 else b"")

    env = Env(handler, min_interval_s=0.0, backoff_base_s=2.0)
    result = env.fetch()
    assert result.status is FetchStatus.OK and result.attempts == 3
    assert env.sleeps == [2.0, 4.0]


def test_retries_exhausted_reports_server_error():
    env = Env(lambda r: httpx.Response(500), min_interval_s=0.0, max_attempts=3)
    result = env.fetch()
    assert result.status is FetchStatus.SERVER_ERROR and result.attempts == 3
    assert len(env.requests) == 3


def test_retry_after_header_is_honoured():
    responses = iter(
        [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, content=b"x")]
    )
    env = Env(lambda r: next(responses), min_interval_s=0.0, backoff_base_s=1.0)
    result = env.fetch()
    assert result.status is FetchStatus.OK
    assert env.sleeps == [7.0]


def test_rate_limited_forever_is_reported():
    env = Env(lambda r: httpx.Response(429), min_interval_s=0.0, max_attempts=2)
    assert env.fetch().status is FetchStatus.RATE_LIMITED


@pytest.mark.parametrize("code", [401, 403, 418])
def test_access_denied_is_final_and_never_retried(code):
    env = Env(lambda r: httpx.Response(code, content=b"nope"))
    result = env.fetch()
    assert result.status is FetchStatus.ACCESS_DENIED and result.http_status == code
    assert result.attempts == 1 and len(env.requests) == 1
    assert "not bypassed" in result.error


def test_not_found_is_not_retried():
    env = Env(lambda r: httpx.Response(404))
    result = env.fetch()
    assert result.status is FetchStatus.NOT_FOUND and len(env.requests) == 1


def test_timeouts_are_retried_then_reported():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    env = Env(handler, min_interval_s=0.0, max_attempts=2)
    result = env.fetch()
    assert result.status is FetchStatus.TIMEOUT and result.attempts == 2


def test_network_errors_are_reported_without_leaking_details():
    def handler(request):
        raise httpx.ConnectError("secret internal detail", request=request)

    env = Env(handler, min_interval_s=0.0, max_attempts=1)
    result = env.fetch()
    assert result.status is FetchStatus.NETWORK_ERROR
    assert result.error == "ConnectError"


def csv_only(body: bytes, ctype: str | None) -> str | None:
    return None if body.startswith(b"SYMBOL") else "not the expected CSV"


def test_http_200_error_page_is_rejected_and_retried():
    bodies = iter([b"<html>Error: too many requests</html>", b"SYMBOL,SERIES\nABC,EQ\n"])
    env = Env(
        lambda r: httpx.Response(200, content=next(bodies)), min_interval_s=0.0, backoff_base_s=3.0
    )
    result = env.fetch(validate=csv_only)
    assert result.status is FetchStatus.OK and result.attempts == 2
    assert env.sleeps == [3.0]


def test_persistent_invalid_content_is_reported_not_returned_as_data():
    env = Env(
        lambda r: httpx.Response(200, content=b"<html>error</html>"),
        min_interval_s=0.0,
        max_attempts=2,
    )
    result = env.fetch(validate=csv_only)
    assert result.status is FetchStatus.INVALID_CONTENT and not result.ok
    assert result.error == "not the expected CSV"


def test_oversized_response_is_aborted():
    env = Env(ok(b"x" * 5000), max_bytes=1000)
    result = env.fetch()
    assert result.status is FetchStatus.TOO_LARGE and result.content is None


def test_every_fetch_is_reported_to_the_recorder_with_its_source():
    env = Env(ok())
    env.fetch()
    ((source, result),) = env.recorded
    assert source == "TEST" and result.status is FetchStatus.OK


# --- robots.txt ------------------------------------------------------------------------------


def robots_site(robots_response: httpx.Response):
    def handler(request):
        if request.url.path == "/robots.txt":
            return robots_response
        return httpx.Response(200, content=b"page", headers={"content-type": "text/html"})

    return handler


def test_robots_disallow_blocks_without_requesting_the_page():
    robots = httpx.Response(
        200, content=b"User-agent: *\nDisallow: /private/", headers={"content-type": "text/plain"}
    )
    env = Env(robots_site(robots), respect_robots=True)
    result = env.fetch("https://data.example.test/private/report.pdf")
    assert result.status is FetchStatus.BLOCKED_BY_ROBOTS
    assert env.hits("/private/report.pdf") == 0
    assert env.fetch("https://data.example.test/public/report.pdf").ok


def test_robots_blocking_our_user_agent_by_name():
    robots = httpx.Response(
        200,
        content=b"User-agent: EquityOS-test\nDisallow: /",
        headers={"content-type": "text/plain"},
    )
    env = Env(robots_site(robots), respect_robots=True)
    assert env.fetch().status is FetchStatus.BLOCKED_BY_ROBOTS


def test_robots_disallow_all_blocks_everything():
    robots = httpx.Response(
        200, content=b"User-agent: *\nDisallow: /", headers={"content-type": "text/plain"}
    )
    env = Env(robots_site(robots), respect_robots=True)
    assert env.fetch().status is FetchStatus.BLOCKED_BY_ROBOTS


def test_missing_robots_means_no_restrictions():
    env = Env(robots_site(httpx.Response(404)), respect_robots=True)
    assert env.fetch().ok


def test_html_app_shell_served_as_robots_is_not_a_policy():
    robots = httpx.Response(
        200, content=b"<html>Disallow: /</html>", headers={"content-type": "text/html"}
    )
    env = Env(robots_site(robots), respect_robots=True)
    assert env.fetch().ok


def test_robots_server_error_is_treated_conservatively():
    env = Env(robots_site(httpx.Response(503)), respect_robots=True)
    result = env.fetch()
    assert result.status is FetchStatus.BLOCKED_BY_ROBOTS
    assert env.hits("/file.csv") == 0


def test_robots_is_fetched_once_per_host():
    robots = httpx.Response(
        200, content=b"User-agent: *\nAllow: /", headers={"content-type": "text/plain"}
    )
    env = Env(robots_site(robots), respect_robots=True, min_interval_s=0.0)
    env.fetch(URL)
    env.fetch(URL + "?a=1")
    assert env.hits("/robots.txt") == 1


# --- cache -----------------------------------------------------------------------------------


def test_cache_serves_repeat_requests_without_network(tmp_path):
    env = Env(ok(b"payload"), cache_dir=tmp_path)
    first = env.fetch(ttl=timedelta(hours=1))
    second = env.fetch(ttl=timedelta(hours=1))
    assert first.status is FetchStatus.OK
    assert (
        second.status is FetchStatus.CACHED and second.from_cache and second.content == b"payload"
    )
    assert len(env.requests) == 1
    assert second.retrieved_at == first.retrieved_at  # provenance of the original download


def test_cache_expires_after_ttl(tmp_path):
    env = Env(ok(b"v1"), cache_dir=tmp_path, min_interval_s=0.0)
    env.fetch(ttl=timedelta(hours=1))
    env.wall += timedelta(hours=2)
    again = env.fetch(ttl=timedelta(hours=1))
    assert again.status is FetchStatus.OK and len(env.requests) == 2


def test_without_ttl_the_cache_is_not_used(tmp_path):
    env = Env(ok(), cache_dir=tmp_path, min_interval_s=0.0)
    env.fetch()
    env.fetch()
    assert len(env.requests) == 2


def test_failures_are_never_cached(tmp_path):
    responses = iter([httpx.Response(403), httpx.Response(200, content=b"fine")])
    env = Env(lambda r: next(responses), cache_dir=tmp_path, min_interval_s=0.0)
    assert env.fetch(ttl=timedelta(hours=1)).status is FetchStatus.ACCESS_DENIED
    assert env.fetch(ttl=timedelta(hours=1)).status is FetchStatus.OK


def test_cached_content_failing_validation_is_refetched(tmp_path):
    env = Env(ok(b"<html>error</html>"), cache_dir=tmp_path, min_interval_s=0.0, max_attempts=1)
    env.fetch(ttl=timedelta(hours=1))  # not cached: it was INVALID_CONTENT
    env.fetch(ttl=timedelta(hours=1), validate=csv_only)
    assert len(env.requests) == 2


def test_corrupt_cache_entry_is_just_a_miss(tmp_path):
    env = Env(ok(b"payload"), cache_dir=tmp_path, min_interval_s=0.0)
    env.fetch(ttl=timedelta(hours=1))
    for meta in tmp_path.rglob("*.json"):
        meta.write_text("{not json")
    assert env.fetch(ttl=timedelta(hours=1)).status is FetchStatus.OK


def test_unwritable_cache_directory_disables_caching_instead_of_crashing(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory")
    env = Env(ok(b"payload"), cache_dir=blocker / "cache", min_interval_s=0.0)  # cannot be created
    first = env.fetch(ttl=timedelta(hours=1))
    second = env.fetch(ttl=timedelta(hours=1))
    assert first.ok and second.ok and not second.from_cache  # works, just without a cache
    assert len(env.requests) == 2
