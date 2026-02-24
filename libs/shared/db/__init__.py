"""Database module for Healthcare Fax Processing System."""

from libs.shared.db.base import Base
from libs.shared.db.session import get_db, get_db_session, init_db

__all__ = ["Base", "get_db", "get_db_session", "init_db"]
