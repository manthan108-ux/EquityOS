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

    # Origins allowed to call the API from a browser (the Next.js dev server by default).
    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once per process."""
    return Settings()
