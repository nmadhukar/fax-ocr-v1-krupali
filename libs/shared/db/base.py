"""
SQLAlchemy declarative base and common utilities.

Uses SQLAlchemy 2.0 patterns with type annotations.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import MetaData, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

# Naming convention for constraints (important for migrations)
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """
    Base class for all SQLAlchemy models.

    Provides:
    - Automatic table naming from class name
    - Common metadata with naming conventions
    - Helper methods for serialization
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Type annotation helpers
    type_annotation_map = {
        UUID: PG_UUID(as_uuid=True),
    }

    @declared_attr.directive
    @classmethod
    def __tablename__(cls) -> str:
        """Generate table name from class name (CamelCase -> snake_case)."""
        name = cls.__name__
        # Convert CamelCase to snake_case
        result = [name[0].lower()]
        for char in name[1:]:
            if char.isupper():
                result.extend(["_", char.lower()])
            else:
                result.append(char)
        return "".join(result)

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """
        Convert model to dictionary.

        Args:
            exclude: Set of column names to exclude.

        Returns:
            Dictionary representation of the model.
        """
        exclude = exclude or set()
        result = {}
        for column in self.__table__.columns:
            if column.name not in exclude:
                value = getattr(self, column.name)
                # Handle special types
                if isinstance(value, datetime):
                    value = value.isoformat()
                elif isinstance(value, UUID):
                    value = str(value)
                result[column.name] = value
        return result

    def __repr__(self) -> str:
        """Generate string representation."""
        pk_columns = [col.name for col in self.__table__.primary_key.columns]
        pk_values = [f"{col}={getattr(self, col)!r}" for col in pk_columns]
        return f"{self.__class__.__name__}({', '.join(pk_values)})"


class TimestampMixin:
    """Mixin for created_at timestamp."""

    created_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        server_default=text("NOW()"),
    )


class UpdateTimestampMixin(TimestampMixin):
    """Mixin for created_at and updated_at timestamps."""

    updated_at: Mapped[datetime] = mapped_column(
        default=datetime.utcnow,
        server_default=text("NOW()"),
        onupdate=datetime.utcnow,
    )
