from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import calc, companies, documents, evidence, health, providers, scoring, sector
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(companies.router)
    app.include_router(providers.router)
    app.include_router(documents.router)
    app.include_router(evidence.router)
    app.include_router(calc.router)
    app.include_router(sector.router)
    app.include_router(scoring.router)
    return app


app = create_app()
