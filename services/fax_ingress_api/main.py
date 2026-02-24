"""
Fax Ingress API - Entry point for fax uploads.

This service handles:
- Fax file uploads (PDF, TIFF, images)
- Storage in MinIO
- Job creation and queuing
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from libs.shared.config import get_settings
from libs.shared.db.session import close_db, get_engine
from libs.shared.security.middleware import apply_security_middleware
from services.fax_ingress_api.api.v1.routes import faxes, health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler."""
    # Startup
    settings = get_settings()
    # Configure structured logging (structlog wraps stdlib logging)
    from libs.shared.logging_config import setup_logging

    setup_logging(
        log_level=settings.api.log_level,
        json_output=settings.environment == "production",
    )
    # Ensure database connection is valid
    get_engine()
    yield
    # Shutdown
    close_db()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Fax Ingress API",
        description="Healthcare Fax Processing System - Ingress Service",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    # CORS middleware — restrict methods and headers in production
    if settings.environment == "production":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.api.allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Security middleware: headers, request-ID, global exception handlers
    apply_security_middleware(
        app,
        production=settings.environment == "production",
        allowed_hosts=settings.api.allowed_hosts,
    )

    # Include routers
    app.include_router(health.router, tags=["Health"])
    app.include_router(faxes.router, prefix="/v1/faxes", tags=["Faxes"])

    # Serve Swagger UI from local static files (no CDN required)
    if settings.api.debug:
        import os
        static_dir = "/app/static"
        if os.path.isdir(static_dir):
            app.mount("/static", StaticFiles(directory=static_dir), name="static")
            swagger_js = "/static/swagger-ui/swagger-ui-bundle.js"
            swagger_css = "/static/swagger-ui/swagger-ui.css"
        else:
            swagger_js = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
            swagger_css = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"

        @app.get("/docs", include_in_schema=False)
        async def swagger_ui() -> HTMLResponse:
            return get_swagger_ui_html(
                openapi_url="/openapi.json",
                title="Fax Ingress API",
                swagger_js_url=swagger_js,
                swagger_css_url=swagger_css,
            )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "main:app",
        host=settings.api.host,
        port=8001,
        reload=settings.api.debug,
    )
