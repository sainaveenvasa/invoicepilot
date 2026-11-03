import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="core.ping")
def ping() -> str:
    """The safe test task: proves a message gets from the broker to a worker.
    It only logs, touching neither the database nor email, so it can run
    anywhere, any number of times."""
    logger.info("pong")
    return "pong"
