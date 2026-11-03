import logging

from celery import current_task


class TaskContextFilter(logging.Filter):
    """Adds the running task to every log line, so a worker's output can be
    followed task by task. Only the name and ID: never the arguments."""

    def filter(self, record):
        task = current_task
        record.task_context = f" task={task.name} task_id={task.request.id}" if task else ""
        return True
