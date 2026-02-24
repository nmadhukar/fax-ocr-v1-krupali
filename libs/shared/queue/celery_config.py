"""
Celery configuration for fax processing workers.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from libs.shared.config import get_settings


@dataclass
class CeleryConfig:
    """Celery configuration settings."""

    # Broker settings
    broker_url: str
    result_backend: str

    # Task settings
    task_serializer: str = "json"
    result_serializer: str = "json"
    accept_content: tuple[str, ...] = ("json",)
    timezone: str = "UTC"
    enable_utc: bool = True

    # Task execution settings
    task_acks_late: bool = True  # Acknowledge after task completes
    task_reject_on_worker_lost: bool = True  # Re-queue if worker dies
    task_time_limit: int = 600  # 10 minutes max per task
    task_soft_time_limit: int = 540  # Soft limit at 9 minutes

    # Worker settings
    worker_prefetch_multiplier: int = 1  # One task at a time for heavy OCR
    worker_concurrency: int = 2  # 2 workers per process

    # Result backend settings
    result_expires: int = 86400  # Results expire after 24 hours

    # Task routing
    task_routes: dict[str, dict[str, str]] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert config to dictionary for Celery."""
        config = {
            "broker_url": self.broker_url,
            "result_backend": self.result_backend,
            "task_serializer": self.task_serializer,
            "result_serializer": self.result_serializer,
            "accept_content": list(self.accept_content),
            "timezone": self.timezone,
            "enable_utc": self.enable_utc,
            "task_acks_late": self.task_acks_late,
            "task_reject_on_worker_lost": self.task_reject_on_worker_lost,
            "task_time_limit": self.task_time_limit,
            "task_soft_time_limit": self.task_soft_time_limit,
            "worker_prefetch_multiplier": self.worker_prefetch_multiplier,
            "worker_concurrency": self.worker_concurrency,
            "result_expires": self.result_expires,
        }

        if self.task_routes:
            config["task_routes"] = self.task_routes

        return config


@lru_cache
def get_celery_config() -> CeleryConfig:
    """
    Get Celery configuration from settings.

    Returns:
        CeleryConfig instance with settings from environment.
    """
    settings = get_settings()

    # Define task routing
    task_routes = {
        # OCR processing tasks go to dedicated queue
        "workers.fax_processing_worker.tasks.process_fax.*": {"queue": "fax_processing"},
        # Template tasks go to default queue
        "workers.fax_processing_worker.tasks.template.*": {"queue": "default"},
    }

    return CeleryConfig(
        broker_url=settings.redis.celery_broker_url,
        result_backend=settings.redis.celery_result_backend,
        task_routes=task_routes,
    )


# Queue names
class Queues:
    """Queue name constants."""

    DEFAULT = "default"
    FAX_PROCESSING = "fax_processing"
    HIGH_PRIORITY = "high_priority"


# Task names
class TaskNames:
    """Task name constants."""

    PROCESS_FAX = "workers.fax_processing_worker.tasks.process_fax.process_fax_task"
    OCR_PAGE = "workers.fax_processing_worker.tasks.process_fax.ocr_page_task"
    TEMPLATE_MATCH = "workers.fax_processing_worker.tasks.template.match_template_task"
