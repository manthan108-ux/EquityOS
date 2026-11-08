"""LLM layer: provider adapters (mocked HTTP), retry/fallback, secret scrubbing, malformed answers."""

import httpx
import pytest

from app.core.config import Settings
from app.llm.base import (
    LlmAuthError,
    LlmBadResponse,
    LlmError,
    LlmNotConfigured,
    LlmRateLimited,
    LlmRequest,
    LlmResponse,
    LlmTransientError,
    parse_json_text,
    scrub,
)
from app.llm.providers import GeminiProvider, GroqProvider, LlmRouter, build_provider

REQ = LlmRequest(system="s", user="u")
GROQ_KEY = "gsk_" + "a" * 40
GEMINI_KEY = "AIza" + "b" * 36


def settings(**kw):
    base = {
        "groq_api_key": GROQ_KEY,
        "gemini_api_key": GEMINI_KEY,
        "_env_file": None,
    }
    base.update(kw)
    return Settings(**base)


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def groq_ok(text='{"a": 1}', finish="stop"):
    return {
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }


def gemini_ok(text='{"a": 1}', finish="STOP"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
    }


# ---- adapters -------------------------------------------------------------------------------------


def test_groq_success_sends_key_in_header_only_and_parses_json():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=groq_ok())

    p = build_provider("groq", settings(), client(handler))
    result = p.complete_json(REQ)
    assert result.data == {"a": 1} and result.provider == "groq" and result.input_tokens == 5
    assert seen["auth"] == f"Bearer {GROQ_KEY}" and GROQ_KEY not in seen["url"]
    assert "json_object" in seen["body"]


def test_gemini_success_sends_key_in_header_not_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-goog-api-key")
        return httpx.Response(200, json=gemini_ok())

    p = build_provider("gemini", settings(), client(handler))
    result = p.complete_json(REQ)
    assert result.data == {"a": 1} and result.output_tokens == 4
    assert seen["key"] == GEMINI_KEY and GEMINI_KEY not in seen["url"]


def test_models_are_configuration_not_code():
    p = build_provider("groq", settings(groq_model="other/model"))
    q = build_provider("gemini", settings(gemini_model="gemini-x"))
    assert p.model == "other/model" and q.model == "gemini-x"
    with pytest.raises(LlmNotConfigured):
        build_provider("nonsense", settings())


def test_missing_key_is_reported_not_called():
    p = build_provider("groq", settings(groq_api_key=None), client(lambda r: pytest.fail("called")))
    assert not p.available()
    with pytest.raises(LlmNotConfigured):
        p.complete_json(REQ)


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, LlmAuthError), (403, LlmAuthError), (429, LlmRateLimited), (503, LlmTransientError)],
)
def test_http_errors_are_classified(status, error):
    p = build_provider("groq", settings(), client(lambda r: httpx.Response(status, text="no")))
    with pytest.raises(error):
        p.complete_json(REQ)


def test_error_messages_never_contain_the_key():
    body = f"bad key {GROQ_KEY} and Bearer {GROQ_KEY} and key={GROQ_KEY}"
    p = build_provider("groq", settings(), client(lambda r: httpx.Response(401, text=body)))
    with pytest.raises(LlmAuthError) as caught:
        p.complete_json(REQ)
    assert GROQ_KEY not in str(caught.value) and "redacted" in str(caught.value)
    assert GEMINI_KEY not in scrub(f"url?key={GEMINI_KEY}")
    assert GROQ_KEY not in scrub(f"x {GROQ_KEY} y", GROQ_KEY)


def test_timeouts_and_connection_errors_are_transient():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    p = build_provider("gemini", settings(), client(boom))
    with pytest.raises(LlmTransientError):
        p.complete_json(REQ)


@pytest.mark.parametrize("text", ["", "not json", "[1,", "42", '"just a string"'])
def test_malformed_model_answers_are_refused(text):
    p = build_provider(
        "groq", settings(), client(lambda r: httpx.Response(200, json=groq_ok(text)))
    )
    with pytest.raises(LlmBadResponse):
        p.complete_json(REQ)


def test_cut_off_answers_are_refused():
    g = build_provider(
        "groq", settings(), client(lambda r: httpx.Response(200, json=groq_ok(finish="length")))
    )
    with pytest.raises(LlmBadResponse, match="cut off"):
        g.complete_json(REQ)
    m = build_provider(
        "gemini",
        settings(),
        client(lambda r: httpx.Response(200, json=gemini_ok(finish="MAX_TOKENS"))),
    )
    with pytest.raises(LlmBadResponse, match="cut off"):
        m.complete_json(REQ)


def test_blocked_gemini_prompt_and_odd_shapes():
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    p = build_provider("gemini", settings(), client(lambda r: httpx.Response(200, json=blocked)))
    with pytest.raises(LlmBadResponse, match="blocked"):
        p.complete_json(REQ)
    q = build_provider(
        "groq", settings(), client(lambda r: httpx.Response(200, json={"choices": []}))
    )
    with pytest.raises(LlmBadResponse):
        q.complete_json(REQ)


def test_json_fence_is_tolerated_but_nothing_else():
    assert parse_json_text('```json\n{"a": 2}\n```') == {"a": 2}
    with pytest.raises(LlmBadResponse):
        parse_json_text("Sure! Here you go: {}")


# ---- router ---------------------------------------------------------------------------------------


class Fake:
    def __init__(self, name, outcomes):
        self.name, self.model, self.outcomes, self.calls = name, "m", list(outcomes), 0

    def available(self):
        return True

    def complete_json(self, request):
        self.calls += 1
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResponse(data=item, raw_text="x", provider=self.name, model="m")


def test_router_retries_transient_then_succeeds():
    primary = Fake("groq", [LlmTransientError("t"), LlmRateLimited("r", 0.0), {"ok": 1}])
    waited = []
    router = LlmRouter([primary], max_attempts=3, sleep=waited.append)
    result = router.complete_json(REQ)
    assert (
        result.data == {"ok": 1}
        and result.attempts == 3
        and primary.calls == 3
        and len(waited) == 2
    )


def test_router_falls_back_after_auth_error_without_retrying_it():
    primary = Fake("groq", [LlmAuthError("denied")])
    backup = Fake("gemini", [{"ok": 2}])
    router = LlmRouter([primary, backup], sleep=lambda s: None)
    result = router.complete_json(REQ)
    assert result.provider == "gemini" and primary.calls == 1
    assert any("LlmAuthError" in n for n in result.notes)


def test_router_falls_back_on_bad_answers_and_reports_when_everything_fails():
    router = LlmRouter(
        [Fake("groq", [LlmBadResponse("x")]), Fake("gemini", [LlmBadResponse("y")])],
        sleep=lambda s: None,
    )
    with pytest.raises(LlmError, match="all LLM providers failed"):
        router.complete_json(REQ)


def test_router_ignores_unconfigured_providers_and_needs_at_least_one():
    class Off(Fake):
        def available(self):
            return False

    assert LlmRouter([Off("groq", [])]).providers == []
    with pytest.raises(LlmNotConfigured):
        LlmRouter([Off("groq", [])]).complete_json(REQ)


def test_rate_limit_waits_are_bounded():
    waited = []
    primary = Fake("groq", [LlmRateLimited("r", 9999.0), {"ok": 1}])
    LlmRouter([primary], sleep=waited.append, max_wait=5.0).complete_json(REQ)
    assert waited == [5.0]


def test_adapters_have_the_provider_interface():
    for cls in (GroqProvider, GeminiProvider):
        assert hasattr(cls, "complete_json") and hasattr(cls, "available")
