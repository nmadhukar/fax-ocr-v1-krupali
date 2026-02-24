"""
JWT authentication for FastAPI endpoints.

Provides Bearer-token authentication via ``Authorization: Bearer <token>``
headers.  In **development** mode (``ENVIRONMENT=development``), a
lightweight bypass allows requests without tokens by injecting a default
dev user, so that local testing stays frictionless.

Production deployments MUST set ``SECRET_KEY`` and ``ENVIRONMENT=production``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from libs.shared.config import get_settings

logger = logging.getLogger(__name__)

# Lazy import — jose is only needed when tokens are actually verified.
_jwt_mod = None


def _jwt():
    global _jwt_mod
    if _jwt_mod is None:
        try:
            from jose import jwt as _j
            _jwt_mod = _j
        except ImportError:
            raise RuntimeError(
                "python-jose[cryptography] is required for JWT auth. "
                "Install with: pip install python-jose[cryptography]"
            )
    return _jwt_mod


# -----------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------

@dataclass(frozen=True)
class AuthUser:
    """Authenticated user context attached to each request."""

    user_id: str
    tenant_id: str
    roles: list[str]

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


# -----------------------------------------------------------------------
# Token creation
# -----------------------------------------------------------------------

def create_access_token(
    user_id: str,
    tenant_id: str,
    roles: list[str] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed JWT access token."""
    settings = get_settings()
    jwt = _jwt()

    now = datetime.now(timezone.utc)
    expire = now + (
        expires_delta
        or timedelta(minutes=settings.security.access_token_expire_minutes)
    )

    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "roles": roles or [],
        "iat": now,
        "exp": expire,
    }
    return jwt.encode(
        payload,
        settings.security.secret_key.get_secret_value(),
        algorithm=settings.security.jwt_algorithm,
    )


# -----------------------------------------------------------------------
# Token verification (FastAPI dependency)
# -----------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


def _decode_token(token: str) -> dict:
    """Decode and validate a JWT token."""
    settings = get_settings()
    jwt = _jwt()

    try:
        payload = jwt.decode(
            token,
            settings.security.secret_key.get_secret_value(),
            algorithms=[settings.security.jwt_algorithm],
        )
        return payload
    except Exception as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _build_user(payload: dict) -> AuthUser:
    """Build AuthUser from JWT payload."""
    user_id = payload.get("sub")
    tenant_id = payload.get("tenant_id")
    if not user_id or not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required claims (sub, tenant_id)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthUser(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=payload.get("roles", []),
    )


# Default dev user (only used when ENVIRONMENT=development)
_DEV_USER = AuthUser(
    user_id="dev-user",
    tenant_id="dev-tenant",
    roles=["admin"],
)


async def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ] = None,
) -> AuthUser:
    """
    FastAPI dependency — returns the authenticated user.

    In development mode, allows unauthenticated requests (returns a
    default dev user).  In production, a valid Bearer token is required.
    """
    settings = get_settings()

    if credentials and credentials.credentials:
        payload = _decode_token(credentials.credentials)
        user = _build_user(payload)
        request.state.user = user
        return user

    # No token provided
    if settings.environment == "development":
        logger.warning(
            "Auth bypass: returning dev user (ENVIRONMENT=development). "
            "Set ENVIRONMENT=production for real authentication."
        )
        request.state.user = _DEV_USER
        return _DEV_USER

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)
    ] = None,
) -> AuthUser | None:
    """
    FastAPI dependency — returns user if token is present, else None.

    Useful for endpoints that behave differently for authenticated
    vs. anonymous users (e.g., health checks).
    """
    if not credentials or not credentials.credentials:
        return None

    try:
        payload = _decode_token(credentials.credentials)
        user = _build_user(payload)
        request.state.user = user
        return user
    except HTTPException:
        return None
