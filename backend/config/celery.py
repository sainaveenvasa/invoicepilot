"""The Celery app, run as ``celery -A config worker`` and ``celery -A config beat``
(system design §8). Configuration is in settings, under the ``CELERY_`` prefix."""

import logging.config
import os
import sys

from celery import Celery
from celery.contrib.django.task import DjangoTask
from celery.signals import setup_logging
from django.conf import settings
from django.db import OperationalError

# As wsgi and asgi: an environment that sets nothing gets the settings that
# refuse to start without their secrets, never the insecure local ones.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")

# macOS pools spawn their processes, where Linux forks them. Celery only sets
# up a spawned process when told to; without this, every task on a Mac fails
# with "not enough values to unpack" while the worker still looks healthy.
if sys.platform == "darwin":
    os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")


class Task(DjangoTask):
    """Base of every task: retries failures that a later attempt can fix, and
    nothing else. A bug or bad data fails once and shows up in the logs.

    ``retry_backoff_max`` stays well below the broker's visibility timeout:
    a longer countdown would be redelivered and run twice (system design §8.1).
    """

    autoretry_for = (ConnectionError, TimeoutError, OperationalError)
    max_retries = 5
    retry_backoff = True  # 1, 2, 4, 8, 16 seconds
    retry_backoff_max = 600
    retry_jitter = True


app = Celery("invoicepilot", task_cls=Task)
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@setup_logging.connect
def use_django_logging(**kwargs):
    """Having a receiver here stops Celery installing its own handlers, so
    workers and beat log through settings.LOGGING, like the web process."""
    logging.config.dictConfig(settings.LOGGING)
