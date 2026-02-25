"""
AuditLog model - HIPAA-compliant audit trail.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from libs.shared.db.base import Base


class AuditLog(Base):
    """
    HIPAA-compliant audit trail for all data access.

    Records all significant actions on PHI for compliance.
    """

    __tablename__ = "audit_log"

    # Primary key (BIGSERIAL for high volume)
    audit_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )

    # Context
    tenant_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(
        String(255),
        default=None,
        index=True,
    )

    # Action details
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Action type: CREATE, READ, UPDATE, DELETE, EXPORT, etc.",
    )
    resource_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="Resource type: fax_job, extraction, template, etc.",
    )
    resource_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        default=None,
        index=True,
    )

    # Additional details
    details: Mapped[dict] = mapped_column(
        JSONB,
        default=dict,
        server_default=text("'{}'::jsonb"),
        comment="Additional action details",
    )

    # Request context
    ip_address: Mapped[str | None] = mapped_column(
        INET,
        default=None,
    )
    user_agent: Mapped[str | None] = mapped_column(
        Text,
        default=None,
    )

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(timezone.utc),
        server_default=text("NOW()"),
        index=True,
    )

    @classmethod
    def log_action(
        cls,
        tenant_id: str,
        action: str,
        resource_type: str,
        user_id: str | None = None,
        resource_id: UUID | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> "AuditLog":
        """
        Create an audit log entry.

        Args:
            tenant_id: Tenant identifier.
            action: Action performed (CREATE, READ, UPDATE, DELETE).
            resource_type: Type of resource accessed.
            user_id: Optional user identifier.
            resource_id: Optional resource UUID.
            details: Optional additional details.
            ip_address: Optional client IP address.
            user_agent: Optional client user agent.

        Returns:
            AuditLog instance (not yet committed).
        """
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            ip_address=ip_address,
            user_agent=user_agent,
        )


# Audit action constants
class AuditAction:
    """Standard audit action types."""

    CREATE = "CREATE"
    READ = "READ"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    EXPORT = "EXPORT"
    DOWNLOAD = "DOWNLOAD"
    PROCESS = "PROCESS"
    REVIEW_CLAIM = "REVIEW_CLAIM"
    REVIEW_SUBMIT = "REVIEW_SUBMIT"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
