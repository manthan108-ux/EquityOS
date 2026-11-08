"""python -m app.llm.cli check   - test each provider on its own (no secrets printed)."""

import argparse
import time

from app.llm.base import LlmError, LlmRequest
from app.llm.providers import build_provider

PROBE = LlmRequest(
    system="You return only JSON.",
    user='Return exactly this JSON object with the number doubled: {"input": 21, "doubled": null}',
    max_output_tokens=400,
)


def cmd_check(args) -> int:
    ok = True
    for name in ("groq", "gemini"):
        provider = build_provider(name)
        if not provider.available():
            print(f"{name:7} not configured (no key)")
            ok = False
            continue
        start = time.time()
        try:
            result = provider.complete_json(PROBE)
            good = isinstance(result.data, dict) and result.data.get("doubled") == 42
            print(
                f"{name:7} OK  model={provider.model}  {time.time() - start:.1f}s  "
                f"tokens in/out={result.input_tokens}/{result.output_tokens}  "
                f"correct answer={'yes' if good else 'NO'}"
            )
            ok = ok and good
        except LlmError as exc:
            print(f"{name:7} FAILED model={provider.model}: {type(exc).__name__}: {exc}")
            ok = False
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.llm.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    args = parser.parse_args(argv)
    return {"check": cmd_check}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
