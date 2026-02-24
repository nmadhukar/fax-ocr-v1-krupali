"""
Production-grade security middleware for FastAPI services.

Adds:
  - Security response headers (HIPAA / OWASP best-practice set)
  - Global exception handler that sanitises error responses in production
    (no stack traces, SQL errors, or internal paths leak to clients)
  - Request-ID injection for distributed tracing
"""

from __future__ import annotations

import logging
import traceback
import uuid
from typing import Callable

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

logger = logging.getLogger(__name__)


# ── Security Headers Middleware ────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Adds OWASP-recommended security headers to every response.

    Headers added:
      X-Content-Type-Options    – prevent MIME sniffing
      X-Frame-Options           – prevent clickjacking
      X-XSS-Protection          – legacy XSS filter (belt-and-suspenders)
      Referrer-Policy           – limit referrer information leakage
      Permissions-Policy        – disable unneeded browser features
      Content-Security-Policy   – strict policy for API (no scripts, frames, etc.)
      Strict-Transport-Security – force HTTPS (production only)
      Cache-Control             – prevent PHI caching in intermediate proxies
    """

    def __init__(self, app, production: bool = False):
        super().__init__(app)
        self._production = production

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Always-on security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # Allow Swagger UI CDN assets for /docs and /redoc; strict otherwise
        path = request.url.path
        if path in ("/docs", "/redoc", "/openapi.json"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
                "img-src 'self' https://fastapi.tiangolo.com data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        # Prevent PHI from being cached by browsers or proxy servers
        response.headers["Cache-Control"] = (
            "no-store, no-cache, must-revalidate, private"
        )
        response.headers["Pragma"] = "no-cache"

        # Production-only: HSTS (only safe once HTTPS is confirmed end-to-end)
        if self._production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        return response


# ── Request-ID Middleware ──────────────────────────────────────────────────────

class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Injects a unique X-Request-ID into every request/response.

    Enables distributed tracing and audit log correlation.
    Respects an incoming X-Request-ID if provided (for upstream tracing).
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# ── Global Exception Handler ───────────────────────────────────────────────────

def add_global_exception_handlers(app: FastAPI, production: bool = False) -> None:
    """
    Register global exception handlers that sanitise error responses.

    In production:
      - 500 errors return a generic message (no internals leaked)
      - Full traceback is only logged server-side

    In development:
      - Details are included in the response for debugging convenience
    """

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "unknown")

        # Always log the full exception server-side
        logger.error(
            "Unhandled exception [request_id=%s] %s %s: %s",
            request_id,
            request.method,
            request.url.path,
            exc,
            exc_info=True,
        )

        if production:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": "An internal server error occurred.",
                    "request_id": request_id,
                },
            )
        else:
            # Include traceback in development for easier debugging
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "detail": str(exc),
                    "type": type(exc).__name__,
                    "request_id": request_id,
                    "traceback": traceback.format_exc().splitlines()[-10:],
                },
            )

    @app.exception_handler(status.HTTP_422_UNPROCESSABLE_ENTITY)
    async def validation_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """Sanitise validation errors — remove internal field paths in production."""
        from fastapi.exceptions import RequestValidationError

        request_id = getattr(request.state, "request_id", "unknown")

        if isinstance(exc, RequestValidationError):
            if production:
                # Generic message only — don't expose field names/types
                return JSONResponse(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    content={
                        "detail": "Request validation failed. Check your request format.",
                        "request_id": request_id,
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "detail": exc.errors(),
                    "request_id": request_id,
                },
            )

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": "Unprocessable entity.", "request_id": request_id},
        )


# ── Convenience: apply all security middleware ─────────────────────────────────

def apply_security_middleware(
    app: FastAPI,
    production: bool = False,
    allowed_hosts: list[str] | None = None,
) -> None:
    """
    Apply all security middleware and exception handlers in the correct order.

    Call this AFTER adding CORS middleware (order matters for starlette):
        app.add_middleware(CORSMiddleware, ...)
        apply_security_middleware(
            app,
            production=settings.environment == "production",
            allowed_hosts=settings.api.allowed_hosts,
        )

    Args:
        app: FastAPI application instance.
        production: Enable production-only hardening (HSTS, sanitised errors).
        allowed_hosts: Restrict incoming Host headers via TrustedHostMiddleware.
            Pass ["*"] or None to allow all hosts (development default).
            In production, pass specific domain names, e.g. ["api.example.com"].
    """
    # Middleware is applied in LIFO order by starlette, so add outermost last

    # TrustedHostMiddleware: only enforce in production with explicit host list
    if production and allowed_hosts and allowed_hosts != ["*"]:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    app.add_middleware(SecurityHeadersMiddleware, production=production)
    app.add_middleware(RequestIDMiddleware)
    add_global_exception_handlers(app, production=production)
