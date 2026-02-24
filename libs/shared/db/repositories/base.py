"""
Base repository with common CRUD operations.

Uses SQLAlchemy 2.0 patterns with type safety.
"""

from typing import Any, Generic, TypeVar
from uuid import UUID

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from libs.shared.db.base import Base

# Type variable for model class
ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """
    Generic base repository with standard CRUD operations.

    Provides a clean interface for database operations while
    maintaining type safety through generics.
    """

    def __init__(self, db: Session, model: type[ModelT]):
        """
        Initialize repository.

        Args:
            db: SQLAlchemy session.
            model: Model class for this repository.
        """
        self.db = db
        self.model = model

    def get_by_id(self, id: UUID) -> ModelT | None:
        """
        Get a single record by primary key.

        Args:
            id: Primary key UUID.

        Returns:
            Model instance or None if not found.
        """
        # Get the primary key column name
        pk_columns = list(self.model.__table__.primary_key.columns)
        if not pk_columns:
            return None

        pk_column = getattr(self.model, pk_columns[0].name)
        stmt = select(self.model).where(pk_column == id)
        return self.db.execute(stmt).scalar_one_or_none()

    def get_all(
        self,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ModelT]:
        """
        Get all records with pagination.

        Args:
            skip: Number of records to skip.
            limit: Maximum records to return.

        Returns:
            List of model instances.
        """
        stmt = select(self.model).offset(skip).limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def count(self) -> int:
        """
        Count all records.

        Returns:
            Total count of records.
        """
        stmt = select(func.count()).select_from(self.model)
        return self.db.execute(stmt).scalar() or 0

    def create(self, obj: ModelT) -> ModelT:
        """
        Create a new record.

        Args:
            obj: Model instance to create.

        Returns:
            Created model instance with generated ID.
        """
        self.db.add(obj)
        self.db.flush()
        return obj

    def create_many(self, objs: list[ModelT]) -> list[ModelT]:
        """
        Create multiple records in bulk.

        Args:
            objs: List of model instances to create.

        Returns:
            List of created model instances.
        """
        self.db.add_all(objs)
        self.db.flush()
        return objs

    def update(self, obj: ModelT, data: dict[str, Any]) -> ModelT:
        """
        Update a record with new data.

        Args:
            obj: Model instance to update.
            data: Dictionary of fields to update.

        Returns:
            Updated model instance.
        """
        for key, value in data.items():
            if hasattr(obj, key):
                setattr(obj, key, value)
        self.db.flush()
        return obj

    def delete(self, obj: ModelT) -> None:
        """
        Delete a record.

        Args:
            obj: Model instance to delete.
        """
        self.db.delete(obj)
        self.db.flush()

    def delete_by_id(self, id: UUID) -> bool:
        """
        Delete a record by primary key.

        Args:
            id: Primary key UUID.

        Returns:
            True if record was deleted, False if not found.
        """
        obj = self.get_by_id(id)
        if obj:
            self.delete(obj)
            return True
        return False

    def exists(self, id: UUID) -> bool:
        """
        Check if a record exists.

        Args:
            id: Primary key UUID.

        Returns:
            True if record exists.
        """
        pk_columns = list(self.model.__table__.primary_key.columns)
        if not pk_columns:
            return False

        pk_column = getattr(self.model, pk_columns[0].name)
        stmt = select(func.count()).select_from(self.model).where(pk_column == id)
        count = self.db.execute(stmt).scalar() or 0
        return count > 0

    def refresh(self, obj: ModelT) -> ModelT:
        """
        Refresh object from database.

        Args:
            obj: Model instance to refresh.

        Returns:
            Refreshed model instance.
        """
        self.db.refresh(obj)
        return obj
