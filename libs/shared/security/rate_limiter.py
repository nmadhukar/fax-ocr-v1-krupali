"""
Simple in-memory rate limiter for FastAPI endpoints.

Uses a sliding-window counter per client IP.  No external dependencies
(no Redis required) — suitable for single-instance deployments.
For multi-instance deployments, swap to a Redis-backed implementation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Token-bucket rate limiter keyed by client IP.

    Args:
        requests_per_minute: Maximum requests allowed per IP per minute.
        burst: Maximum burst size (instant requests before throttling).
    """

    def __init__(
        self,
        requests_per_minute: int = 60,
        burst: int = 10,
    ):
        self.rate = requests_per_minute / 60.0  # tokens per second
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_time)
        self._lock = Lock()

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        if request.client:
            return request.client.host
        return "unknown"

    def check(self, request: Request) -> None:
        """
        Check rate limit for the request. Raises 429 if exceeded.

        Args:
            request: The incoming FastAPI request.

        Raises:
            HTTPException 429 if rate limit is exceeded.
        """
        ip = self._get_client_ip(request)
        now = time.monotonic()

        with self._lock:
            if ip in self._buckets:
                tokens, last_time = self._buckets[ip]
                # Refill tokens based on elapsed time
                elapsed = now - last_time
                tokens = min(self.burst, tokens + elapsed * self.rate)
            else:
                tokens = float(self.burst)

            if tokens < 1.0:
                logger.warning("Rate limit exceeded for IP %s", ip)
                self._buckets[ip] = (tokens, now)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Rate limit exceeded. Please try again later.",
                    headers={"Retry-After": str(int(1.0 / self.rate))},
                )

            # Consume one token
            self._buckets[ip] = (tokens - 1.0, now)

    def cleanup(self, max_age_seconds: float = 300.0) -> int:
        """Remove stale entries older than max_age_seconds. Returns count removed."""
        now = time.monotonic()
        removed = 0
        with self._lock:
            stale = [
                ip for ip, (_, last) in self._buckets.items()
                if now - last > max_age_seconds
            ]
            for ip in stale:
                del self._buckets[ip]
                removed += 1
        return removed


# Singleton instances for each service
_upload_limiter = RateLimiter(requests_per_minute=30, burst=5)
_api_limiter = RateLimiter(requests_per_minute=120, burst=20)


def _start_cleanup_thread(interval_seconds: float = 300.0) -> None:
    """
    Start a background daemon thread that periodically removes stale buckets.

    Runs every `interval_seconds` (default 5 min).  Daemon threads are
    automatically killed when the main process exits, so no explicit
    shutdown is required.
    """
    def _cleanup_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                removed_upload = _upload_limiter.cleanup()
                removed_api = _api_limiter.cleanup()
                if removed_upload or removed_api:
                    logger.debug(
                        "Rate limiter cleanup: removed %d upload + %d API stale buckets",
                        removed_upload,
                        removed_api,
                    )
            except Exception:
                logger.exception("Rate limiter cleanup error (non-fatal)")

    t = threading.Thread(target=_cleanup_loop, name="rate-limiter-cleanup", daemon=True)
    t.start()


# Start cleanup thread when module is first imported
_start_cleanup_thread()


def get_upload_rate_limiter() -> RateLimiter:
    """Rate limiter for upload endpoints (stricter)."""
    return _upload_limiter


def get_api_rate_limiter() -> RateLimiter:
    """Rate limiter for general API endpoints."""
    return _api_limiter
