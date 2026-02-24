"""
HIPAA-compliant audit logging middleware and utilities.

Automatically logs all API requests that access PHI (Protected Health
Information) to the ``audit_log`` database table.  Captures:
  - User identity (from JWT)
  - Action type (CREATE, READ, UPDATE, DELETE)
  - Resource type and ID
  - Client IP address and user-agent
  - Timestamp
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from libs.shared.db.models.audit_log import AuditLog, AuditAction

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    Structured audit logger that writes to the ``audit_log`` table.

    Usage in route handlers::

        audit = get_audit_logger(request, db)
        audit.log_read("fax_job", fax_job_id)
    """

    def __init__(self, request: Request, db: Session):
        self._request = request
        self._db = db

        # Extract user info from request.state (set by auth middleware)
        user = getattr(request.state, "user", None)
        self._user_id: str | None = getattr(user, "user_id", None)
        self._tenant_id: str = getattr(user, "tenant_id", "unknown")

        # Client context
        self._ip = self._get_client_ip(request)
        self._user_agent = request.headers.get("user-agent", "")[:500]

    @staticmethod
    def _get_client_ip(request: Request) -> str | None:
        """Extract client IP, checking X-Forwarded-For for reverse proxies."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return None

    def _write(
        self,
        action: str,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        """Write an audit log entry (best-effort, never raises)."""
        try:
            entry = AuditLog.log_action(
                tenant_id=self._tenant_id,
                action=action,
                resource_type=resource_type,
                user_id=self._user_id,
                resource_id=resource_id,
                details=details,
                ip_address=self._ip,
                user_agent=self._user_agent,
            )
            self._db.add(entry)
            self._db.flush()
        except Exception:
            logger.warning(
                "Audit log write failed for %s %s/%s",
                action, resource_type, resource_id,
                exc_info=True,
            )

    # Convenience methods for common actions
    def log_create(
        self,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.CREATE, resource_type, resource_id, details)

    def log_read(
        self,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.READ, resource_type, resource_id, details)

    def log_update(
        self,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.UPDATE, resource_type, resource_id, details)

    def log_delete(
        self,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.DELETE, resource_type, resource_id, details)

    def log_download(
        self,
        resource_type: str,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.DOWNLOAD, resource_type, resource_id, details)

    def log_review_claim(
        self,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.REVIEW_CLAIM, "fax_review", resource_id, details)

    def log_review_submit(
        self,
        resource_id: UUID | None = None,
        details: dict | None = None,
    ) -> None:
        self._write(AuditAction.REVIEW_SUBMIT, "fax_review", resource_id, details)


def get_audit_logger(request: Request, db: Session) -> AuditLogger:
    """Factory function for creating AuditLogger in route handlers."""
    return AuditLogger(request, db)
