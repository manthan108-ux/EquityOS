from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import check_database

router = APIRouter(tags=["health"])


class LiveResponse(BaseModel):
    status: str
    service: str
    version: str


class ReadyResponse(BaseModel):
    status: str
    database: str
    # Only the exception class name is exposed, never connection strings or messages.
    detail: str | None = None


@router.get("/health", response_model=LiveResponse)
def liveness() -> LiveResponse:
    """Liveness: the process is up. Does not touch the database."""
    settings = get_settings()
    return LiveResponse(status="ok", service=settings.app_name, version=settings.app_version)


@router.get("/health/ready", response_model=ReadyResponse)
def readiness(response: Response) -> ReadyResponse:
    """Readiness: the service can do real work, i.e. the database answers."""
    ok, error = check_database()
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadyResponse(status="unavailable", database="down", detail=error)
    return ReadyResponse(status="ok", database="up")
