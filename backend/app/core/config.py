"""Application settings, loaded from environment variables (and an optional .env file).

Secrets never live in code: every value below can be overridden by an env var
named exactly like the field, upper-cased (e.g. DATABASE_URL).
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# The single .env lives at the repository root (backend/app/core/config.py -> ../../..).
# Inside Docker the file is absent and real environment variables are used instead.
_ROOT_ENV = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ROOT_ENV, env_file_encoding="utf-8", extra="ignore")

    app_name: str = "EquityOS API"
    app_version: str = "0.1.0"
    environment: str = "development"

    # SQLAlchemy URL using the psycopg (v3) driver.
    database_url: str = "postgresql+psycopg://equityos:equityos@localhost:5432/equityos"

    # Download/cache root (git-ignored); relative paths start at the repo root.
    data_dir: str = "data"
    # Honest, descriptive identification sent to every site we contact.
    http_user_agent: str = "EquityOS-research/0.1 (+https://github.com/manthan108-ux/EquityOS)"

    # Optional provider credentials. Never logged, cached or committed: set them in .env.
    # Upstox free read-only Analytics Token (1 year), from the Upstox Developer Apps page.
    upstox_analytics_token: str | None = None
    upstox_access_token: str | None = None  # alternative: a normal daily OAuth access token
    # Feature flag: set UPSTOX_ENABLED=false to switch the connector off even when a token is set.
    # Recomputing stored scores/benchmarks over HTTP is off until an authentication layer exists.
    scoring_admin_enabled: bool = False
    upstox_enabled: bool = True
    # Research workflow (Phase 11). API clients are "name:secret" pairs, comma-separated; the
    # secret is compared by hash and never logged. Roles: "name:secret:admin" for an admin.
    research_api_keys: str = ""
    research_max_upload_mb: int = 50
    research_rate_limit_per_minute: int = 30  # per client, for upload/report endpoints
    research_upload_rate_per_hour: int = 20  # uploads per client
    data_gov_in_api_key: str | None = None  # free key from data.gov.in

    # LLM providers (free tiers). Keys live only in .env; model names are configuration, not code.
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    gemini_model: str = "gemini-3.5-flash"
    llm_primary: str = "groq"  # groq | gemini ; the other one is the fallback
    llm_timeout_seconds: float = 90.0
    llm_max_attempts: int = 3

    # Origins allowed to call the API from a browser (the Next.js dev server by default).
    cors_origins: list[str] = ["http://localhost:3000"]


def resolve_data_dir(settings: Settings) -> Path:
    path = Path(settings.data_dir)
    return path if path.is_absolute() else _ROOT_ENV.parent / path


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once per process."""
    return Settings()
