"""
Database session management.

Provides SQLAlchemy engine and session factory with proper
connection pooling and transaction handling.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from libs.shared.config import get_settings
from libs.shared.db.base import Base

# Global engine and session factory
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """
    Get or create the SQLAlchemy engine.

    Returns:
        SQLAlchemy Engine instance.
    """
    global _engine

    if _engine is None:
        settings = get_settings()
        _engine = create_engine(
            settings.database.database_url,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=settings.database.pool_timeout,
            pool_pre_ping=True,  # Enable connection health checks
            echo=settings.database.echo_sql,
        )

        # Register event listeners for connection management
        @event.listens_for(_engine, "connect")
        def set_search_path(dbapi_conn: Any, connection_record: Any) -> None:
            """Set schema search path on new connections."""
            cursor = dbapi_conn.cursor()
            cursor.execute("SET search_path TO public")
            cursor.close()

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """
    Get or create the session factory.

    Returns:
        SQLAlchemy sessionmaker instance.
    """
    global _session_factory

    if _session_factory is None:
        engine = get_engine()
        _session_factory = sessionmaker(
            bind=engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    return _session_factory


def get_db() -> Generator[Session, None, None]:
    """
    Dependency for FastAPI to get database session.

    Yields:
        SQLAlchemy Session instance.

    Example:
        @app.get("/items")
        def get_items(db: Session = Depends(get_db)):
            return db.query(Item).all()
    """
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """
    Context manager for database session.

    Use this for non-FastAPI contexts (e.g., Celery workers).

    Yields:
        SQLAlchemy Session instance.

    Example:
        with get_db_session() as db:
            job = db.query(FaxJob).get(job_id)
            job.status = "COMPLETED"
    """
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """
    Initialize database tables.

    Creates all tables defined in Base.metadata.
    Should only be used for development/testing.
    """
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def close_db() -> None:
    """
    Close database connections.

    Should be called on application shutdown.
    """
    global _engine, _session_factory

    if _engine is not None:
        _engine.dispose()
        _engine = None
        _session_factory = None
