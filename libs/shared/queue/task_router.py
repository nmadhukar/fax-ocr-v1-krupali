"""
Task routing and priority management.
"""

from typing import Any

from libs.shared.queue.celery_config import Queues


class TaskRouter:
    """
    Routes tasks to appropriate queues based on task type and priority.
    """

    # Default queue for each task type
    TASK_QUEUE_MAP = {
        "process_fax": Queues.FAX_PROCESSING,
        "ocr_page": Queues.FAX_PROCESSING,
        "template_match": Queues.DEFAULT,
        "extract_fields": Queues.FAX_PROCESSING,
        "validate_extraction": Queues.DEFAULT,
        "create_review": Queues.DEFAULT,
    }

    # Priority levels (lower number = higher priority)
    class Priority:
        URGENT = 0
        HIGH = 3
        NORMAL = 6
        LOW = 9

    @classmethod
    def get_queue(cls, task_name: str) -> str:
        """
        Get the queue for a task.

        Args:
            task_name: Name of the task.

        Returns:
            Queue name.
        """
        # Extract task type from full task name
        task_type = task_name.split(".")[-1].replace("_task", "")
        return cls.TASK_QUEUE_MAP.get(task_type, Queues.DEFAULT)

    @classmethod
    def route_task(
        cls,
        task_name: str,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Route a task to the appropriate queue.

        This method can be used as a Celery task router.

        Args:
            task_name: Full task name.
            args: Task positional arguments.
            kwargs: Task keyword arguments.

        Returns:
            Dictionary with queue and other routing info.
        """
        queue = cls.get_queue(task_name)

        # Check for priority override in kwargs (don't mutate caller's dict)
        priority = cls.Priority.NORMAL
        if kwargs and "priority" in kwargs:
            priority = kwargs["priority"]

        return {
            "queue": queue,
            "priority": priority,
        }

    @classmethod
    def get_task_options(
        cls,
        task_name: str,
        priority: int | None = None,
        countdown: int | None = None,
        eta: Any | None = None,
    ) -> dict[str, Any]:
        """
        Get task options for applying to a task.

        Args:
            task_name: Task name.
            priority: Optional priority override.
            countdown: Optional countdown in seconds.
            eta: Optional ETA for task execution.

        Returns:
            Dictionary of task options.
        """
        options: dict[str, Any] = {
            "queue": cls.get_queue(task_name),
        }

        if priority is not None:
            options["priority"] = priority

        if countdown is not None:
            options["countdown"] = countdown

        if eta is not None:
            options["eta"] = eta

        return options
