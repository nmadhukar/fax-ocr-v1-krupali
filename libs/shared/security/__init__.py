"""Security module — JWT auth, audit logging, file validation, rate limiting."""

from libs.shared.security.auth import (
    AuthUser,
    get_current_user,
    get_optional_user,
    create_access_token,
)
from libs.shared.security.audit import AuditLogger, get_audit_logger
from libs.shared.security.file_validation import (
    validate_file_magic,
    sanitize_filename,
    MAX_FILENAME_LENGTH,
)
from libs.shared.security.rate_limiter import (
    RateLimiter,
    get_upload_rate_limiter,
    get_api_rate_limiter,
)

__all__ = [
    "AuthUser",
    "get_current_user",
    "get_optional_user",
    "create_access_token",
    "AuditLogger",
    "get_audit_logger",
    "validate_file_magic",
    "sanitize_filename",
    "MAX_FILENAME_LENGTH",
    "RateLimiter",
    "get_upload_rate_limiter",
    "get_api_rate_limiter",
]
