"""
Celery application configuration for fax processing worker.
"""

from celery import Celery

from libs.shared.logging_config import setup_logging
from libs.shared.queue.celery_config import get_celery_config

# Configure structured logging for worker
setup_logging(log_level="INFO", json_output=False)

# Get configuration
config = get_celery_config()

# Create Celery application
app = Celery(
    "fax_processing_worker",
    include=[
        "workers.fax_processing_worker.tasks.process_fax",
        "workers.fax_processing_worker.tasks.mismatch_monitor",
        "workers.fax_processing_worker.tasks.retrain_layoutlm",
    ],
)

# Apply configuration
app.config_from_object(config.to_dict())

# Additional configuration
app.conf.update(
    # Task settings
    task_track_started=True,
    task_default_queue="fax_processing",

    # Worker settings
    worker_max_tasks_per_child=50,  # Restart worker after 50 tasks (memory management)
    worker_max_memory_per_child=2000000,  # 2GB memory limit

    # Beat schedule
    beat_schedule={
        "mismatch-monitor-hourly": {
            "task": "workers.fax_processing_worker.tasks.mismatch_monitor.aggregate_mismatch_metrics",
            "schedule": 3600.0,  # every hour
        },
        "layoutlm-retrain-weekly": {
            "task": "workers.fax_processing_worker.tasks.retrain_layoutlm.check_and_retrain_layoutlm",
            "schedule": 604800.0,  # every 7 days
            # Override schedule via env: RETRAIN_MIN_NEW_LABELS (default 20)
        },
    },
)


if __name__ == "__main__":
    app.start()
