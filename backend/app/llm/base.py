"""Provider-neutral request/response types and the error taxonomy.

Errors never carry secrets: provider messages are passed through `scrub` (API keys, bearer
tokens and anything that looks like a key are replaced) before they reach an exception or a log.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Protocol

_SECRET = re.compile(
    r"(AIza[0-9A-Za-z_\-]{20,}|gsk_[0-9A-Za-z]{20,}|sk-[0-9A-Za-z_\-]{20,}"
    r"|Bearer\s+[0-9A-Za-z._\-]{16,}|(?i:key|token)=[^&\s\"']{8,})"
)


def scrub(text: str, *secrets: str | None) -> str:
    out = _SECRET.sub("[redacted]", text or "")
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[redacted]")
    return out[:500]


class LlmError(Exception):
    """Base error. `retryable` tells the caller whether another attempt can help."""

    retryable = False


class LlmNotConfigured(LlmError):
    pass


class LlmAuthError(LlmError):
    pass


class LlmRateLimited(LlmError):
    retryable = True

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LlmTransientError(LlmError):  # timeouts, 5xx, connection problems
    retryable = True


class LlmBadResponse(LlmError):  # not JSON / empty / blocked / cut off
    pass


@dataclass(frozen=True)
class LlmRequest:
    system: str
    user: str
    max_output_tokens: int = 4096
    temperature: float = 0.0


@dataclass(frozen=True)
class LlmResponse:
    data: dict | list  # the parsed JSON
    raw_text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    attempts: int = 1
    notes: list[str] = field(default_factory=list)


class LlmProvider(Protocol):
    name: str
    model: str

    def available(self) -> bool: ...

    def complete_json(self, request: LlmRequest) -> LlmResponse: ...


def parse_json_text(text: str) -> dict | list:
    """Parse a model's JSON answer; tolerates a ```json fence, nothing else."""
    cleaned = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    if not cleaned:
        raise LlmBadResponse("the model returned no text")
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmBadResponse(f"the model's answer is not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict | list):
        raise LlmBadResponse("the model's JSON is neither an object nor a list")
    return data
