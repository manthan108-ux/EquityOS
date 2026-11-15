"""Create a research API client without touching source code.

    python -m app.research.keys_cli add NAME [--admin]

The secret is random (256 bits) and is appended to RESEARCH_API_KEYS in the git-ignored `.env`.
It is not printed unless --show is given, and never logged. The server keeps only its SHA-256 digest
in memory (see security.py); restart the backend/worker after changing `.env`.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

from app.research.security import configured_clients

ENV = Path(__file__).resolve().parents[3] / ".env"
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def add_client(env_path: Path, name: str, *, admin: bool) -> str:
    """Append a client with a fresh secret to RESEARCH_API_KEYS; returns the secret."""
    if not NAME.match(name):
        raise ValueError("a client name is letters, digits, - or _ (up to 40 characters)")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    idx = next((i for i, ln in enumerate(lines) if ln.startswith("RESEARCH_API_KEYS=")), None)
    current = lines[idx].split("=", 1)[1].strip().strip("\"'") if idx is not None else ""
    existing = [p.split(":")[0].strip() for p in current.split(",") if p.strip()]
    if name in existing:
        raise ValueError(f"a client named {name} already exists; choose another name")
    secret = secrets.token_urlsafe(32)
    entry = f"{name}:{secret}" + (":admin" if admin else "")
    value = f"{current},{entry}" if current else entry
    if idx is None:
        lines.append(f"RESEARCH_API_KEYS={value}")
    else:
        lines[idx] = f"RESEARCH_API_KEYS={value}"
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return secret


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="keys_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="create a client with a new random key")
    p.add_argument("name")
    p.add_argument("--admin", action="store_true")
    p.add_argument(
        "--show", action="store_true", help="print the new key once (it is stored in .env)"
    )
    p.add_argument("--env", type=Path, default=ENV)
    sub.add_parser("list", help="client names and roles (never the keys)")
    args = ap.parse_args(argv)
    if args.cmd == "list":
        for n, _d, admin in configured_clients():
            print(n, "admin" if admin else "")
        return 0
    try:
        secret = add_client(args.env, args.name, admin=args.admin)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    role = "admin " if args.admin else ""
    print(f"added {role}client {args.name} to {args.env.name}; restart the backend")
    if args.show:
        print(secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
