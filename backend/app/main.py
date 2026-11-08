from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import companies, documents, evidence, health, providers
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=settings.app_version)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(companies.router)
    app.include_router(providers.router)
    app.include_router(documents.router)
    app.include_router(evidence.router)
    return app


app = create_app()
