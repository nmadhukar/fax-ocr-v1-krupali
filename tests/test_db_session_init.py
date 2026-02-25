"""
Regression tests for DB session/engine bootstrap.
"""

from __future__ import annotations

import threading

from libs.shared.db import session as db_session


def test_get_session_factory_does_not_deadlock_on_first_init(monkeypatch) -> None:
    """
    First-time session factory init must not deadlock.

    Regression target: get_session_factory() used to acquire a non-reentrant
    lock and then call get_engine(), which attempted to acquire the same lock.
    """
    original_engine = db_session._engine
    original_factory = db_session._session_factory
    try:
        db_session._engine = None
        db_session._session_factory = None

        class _DB:
            database_url = "sqlite:///:memory:"
            pool_size = 1
            max_overflow = 1
            pool_timeout = 1
            echo_sql = False

        class _Settings:
            database = _DB()

        monkeypatch.setattr(db_session, "get_settings", lambda: _Settings())
        monkeypatch.setattr(db_session.event, "listens_for", lambda *args, **kwargs: (lambda fn: fn))

        class _Engine:
            pass

        engine = _Engine()
        monkeypatch.setattr(db_session, "create_engine", lambda *args, **kwargs: engine)
        monkeypatch.setattr(
            db_session,
            "sessionmaker",
            lambda **kwargs: {"bind": kwargs["bind"], "autoflush": kwargs["autoflush"]},
        )

        result: dict[str, object] = {}
        error: list[Exception] = []

        def _runner() -> None:
            try:
                result["factory"] = db_session.get_session_factory()
            except Exception as exc:  # pragma: no cover - defensive
                error.append(exc)

        worker = threading.Thread(target=_runner, daemon=True)
        worker.start()
        worker.join(timeout=1.0)

        assert not worker.is_alive(), "get_session_factory() deadlocked during first init"
        assert not error
        assert result["factory"]["bind"] is engine
    finally:
        db_session._engine = original_engine
        db_session._session_factory = original_factory
