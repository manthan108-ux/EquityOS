"""Groq and Gemini over plain HTTPS (httpx).

Keys go in headers, never in URLs, and never in error messages.
"""

import time
from collections.abc import Callable

import httpx

from app.core.config import Settings, get_settings
from app.llm.base import (
    LlmAuthError,
    LlmBadResponse,
    LlmError,
    LlmNotConfigured,
    LlmProvider,
    LlmRateLimited,
    LlmRequest,
    LlmResponse,
    LlmTransientError,
    parse_json_text,
    scrub,
)

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers.get("retry-after", ""))
    except ValueError:
        return None


def _raise_for_status(response: httpx.Response, secret: str | None) -> None:
    code = response.status_code
    if code < 400:
        return
    detail = scrub(response.text, secret)
    if code in (401, 403):
        raise LlmAuthError(f"authentication refused (HTTP {code}): {detail}")
    if code == 429:
        raise LlmRateLimited(f"rate limit or quota reached: {detail}", _retry_after(response))
    if code >= 500 or code == 408:
        raise LlmTransientError(f"provider error (HTTP {code}): {detail}")
    raise LlmError(f"request rejected (HTTP {code}): {detail}")


class _Base:
    name = ""

    def __init__(
        self, key: str | None, model: str, settings: Settings, client: httpx.Client | None
    ):
        self._key = key
        self.model = model
        self._timeout = settings.llm_timeout_seconds
        self._client = client

    def available(self) -> bool:
        return bool(self._key)

    def _post(self, url: str, headers: dict, body: dict) -> httpx.Response:
        if not self._key:
            raise LlmNotConfigured(f"{self.name}: no API key is set")
        try:
            if self._client is not None:
                return self._client.post(url, headers=headers, json=body, timeout=self._timeout)
            return httpx.post(url, headers=headers, json=body, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise LlmTransientError(f"{self.name}: request timed out") from exc
        except httpx.HTTPError as exc:
            raise LlmTransientError(
                f"{self.name}: connection problem ({type(exc).__name__})"
            ) from exc


class GroqProvider(_Base):
    name = "groq"

    def complete_json(self, request: LlmRequest) -> LlmResponse:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_completion_tokens": request.max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        response = self._post(GROQ_URL, {"Authorization": f"Bearer {self._key}"}, body)
        _raise_for_status(response, self._key)
        try:
            payload = response.json()
            choice = payload["choices"][0]
            text = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmBadResponse("groq: unexpected response shape") from exc
        if choice.get("finish_reason") == "length":
            raise LlmBadResponse("groq: the answer was cut off (output limit)")
        usage = payload.get("usage") or {}
        return LlmResponse(
            data=parse_json_text(text),
            raw_text=text,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


class GeminiProvider(_Base):
    name = "gemini"

    def complete_json(self, request: LlmRequest) -> LlmResponse:
        body = {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": {
                "temperature": request.temperature,
                "maxOutputTokens": request.max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        url = GEMINI_URL.format(model=self.model)
        response = self._post(url, {"x-goog-api-key": self._key or ""}, body)
        _raise_for_status(response, self._key)
        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmBadResponse("gemini: response is not JSON") from exc
        candidates = payload.get("candidates") or []
        if not candidates:
            block = (payload.get("promptFeedback") or {}).get("blockReason")
            suffix = f" (blocked: {block})" if block else ""
            raise LlmBadResponse(f"gemini: no answer returned{suffix}")
        candidate = candidates[0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise LlmBadResponse("gemini: the answer was cut off (output limit)")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = payload.get("usageMetadata") or {}
        return LlmResponse(
            data=parse_json_text(text),
            raw_text=text,
            provider=self.name,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )


def build_provider(
    name: str, settings: Settings | None = None, client: httpx.Client | None = None
) -> LlmProvider:
    settings = settings or get_settings()
    if name == "groq":
        return GroqProvider(settings.groq_api_key, settings.groq_model, settings, client)
    if name == "gemini":
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model, settings, client)
    raise LlmNotConfigured(f"unknown LLM provider {name!r}")


class LlmRouter:
    """Primary provider with retry/backoff, then the other as fallback.

    Rate limits wait (bounded) and retry; auth errors and bad answers do not retry the same
    provider but do fall through to the other one. Every failure is recorded in `notes`.
    """

    def __init__(
        self,
        providers: list[LlmProvider],
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 20.0,
    ):
        self.providers = [p for p in providers if p.available()]
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._max_wait = max_wait

    def complete_json(self, request: LlmRequest) -> LlmResponse:
        if not self.providers:
            raise LlmNotConfigured("no LLM provider has an API key")
        notes: list[str] = []
        last: LlmError | None = None
        for provider in self.providers:
            for attempt in range(1, self.max_attempts + 1):
                try:
                    result = provider.complete_json(request)
                    return LlmResponse(
                        data=result.data,
                        raw_text=result.raw_text,
                        provider=result.provider,
                        model=result.model,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        attempts=attempt,
                        notes=notes,
                    )
                except LlmError as exc:
                    last = exc
                    notes.append(f"{provider.name} attempt {attempt}: {type(exc).__name__}: {exc}")
                    if not exc.retryable or attempt == self.max_attempts:
                        break
                    wait = getattr(exc, "retry_after", None) or min(2**attempt, self._max_wait)
                    self._sleep(min(wait, self._max_wait))
        raise LlmError("all LLM providers failed: " + " | ".join(notes[-4:])) from last


def default_router(settings: Settings | None = None) -> LlmRouter:
    settings = settings or get_settings()
    order = ["groq", "gemini"] if settings.llm_primary == "groq" else ["gemini", "groq"]
    return LlmRouter([build_provider(n, settings) for n in order], settings.llm_max_attempts)
