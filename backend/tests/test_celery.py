"""Background work (system design §8, threat model T11).

Nothing here touches Redis: the test settings use an in-process broker and
run tasks inline. No test asks for the ``db`` fixture, so pytest-django fails
any of them that reaches the database.
"""

import logging
import os
import sys

import pytest
from celery import current_app
from celery.exceptions import QueueNotFound
from celery.schedules import crontab
from celery.signals import setup_logging
from django.conf import settings
from django.core import mail
from django.db import OperationalError

from apps.core.tasks import ping
from config import celery_app
from config.celery import Task
from config.logging import TaskContextFilter

celery_app.loader.import_default_modules()

QUEUES = {"outbox", "email", "default", "imports", "maintenance"}


def queue_of(task_name):
    return celery_app.amqp.router.route({}, task_name)["queue"].name


# Configuration


def test_tasks_use_the_project_app_and_never_redis_in_tests():
    assert current_app.main == "invoicepilot"
    assert celery_app.conf.broker_url == "memory://"


def test_celery_accepts_json_only():
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.result_serializer == "json"


def test_an_interrupted_task_is_redelivered():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1


def test_results_are_not_stored():
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.result_backend is None


def test_time_limits():
    assert celery_app.conf.task_soft_time_limit == 240
    assert celery_app.conf.task_time_limit == 300


@pytest.mark.parametrize("task_name", ["imports.validate_batch", "imports.commit_batch"])
def test_imports_get_a_longer_time_limit(task_name):
    limits = celery_app.conf.task_annotations[task_name]
    assert limits["soft_time_limit"] < limits["time_limit"] == 600


def test_a_running_or_retrying_task_is_never_redelivered_by_the_visibility_timeout():
    """A message unacknowledged for longer than the timeout is delivered again, and would run twice."""
    visibility_timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    annotated = [limits["time_limit"] for limits in celery_app.conf.task_annotations.values()]
    assert max(celery_app.conf.task_time_limit, *annotated) < visibility_timeout
    assert Task.retry_backoff_max < visibility_timeout


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS pools spawn their processes; Linux forks them")
def test_macos_workers_set_up_their_spawned_processes():
    """Without this every task fails there, which inline tests can't see."""
    assert os.environ["FORKED_BY_MULTIPROCESSING"] == "1"


# Routing


def test_the_five_queues_are_declared():
    assert {queue.name for queue in celery_app.conf.task_queues} == QUEUES
    assert celery_app.conf.task_default_queue == "default"


def test_every_route_points_at_a_declared_queue():
    assert {route["queue"] for route in celery_app.conf.task_routes.values()} <= QUEUES


@pytest.mark.parametrize(
    ("task_name", "queue"),
    [
        ("outbox.relay_batch", "outbox"),
        ("outbox.recover_leases", "outbox"),
        ("reminders.send_delivery", "email"),
        ("accounts.send_verification", "email"),
        ("imports.validate_batch", "imports"),
        ("imports.commit_batch", "imports"),
        ("invoices.reconcile_balances", "maintenance"),
        ("billing.reconcile_subscriptions", "maintenance"),
        ("reminders.reconcile_stuck_deliveries", "maintenance"),
        ("retention.run", "maintenance"),
        ("core.ping", "maintenance"),
        ("webhooks.sweep", "default"),
        ("reminders.plan_all", "default"),
        ("reminders.claim_due_deliveries", "default"),
    ],
)
def test_tasks_are_routed_to_their_queue(task_name, queue):
    """System design §8.2. Tasks not built yet are routed by name, so they land correctly from day one."""
    assert queue_of(task_name) == queue


def test_a_mistyped_queue_is_an_error_not_a_silent_new_queue():
    assert celery_app.conf.task_create_missing_queues is False
    with pytest.raises(QueueNotFound):
        celery_app.amqp.router.route({"queue": "emial"}, "core.ping")


# The test task


def test_ping_runs_and_answers():
    assert ping.delay().get() == "pong"


def test_ping_sends_no_email():
    ping.delay()
    assert mail.outbox == []


# Beat


def test_beat_only_schedules_tasks_that_exist():
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert scheduled <= set(celery_app.tasks)


def test_scheduled_tasks_expire_rather_than_pile_up():
    entries = celery_app.conf.beat_schedule.values()
    interval_entries = [entry for entry in entries if not isinstance(entry["schedule"], crontab)]
    assert all(entry["options"]["expires"] <= entry["schedule"] for entry in interval_entries)


def test_beat_keeps_its_state_out_of_the_working_directory():
    assert str(settings.BASE_DIR) not in celery_app.conf.beat_schedule_filename


def test_nothing_can_send_a_reminder_yet():
    """A tripwire: sending stays impossible until the delivery table's eligibility,
    idempotency and caps exist (threat model T11). Replace this test with
    ``test_send_delivery_rechecks_eligibility_when_invoked_directly`` when they do."""
    assert not [name for name in celery_app.tasks if name.startswith("reminders.")]
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    assert not {task for task in scheduled if task.startswith("reminders.") or queue_of(task) == "email"}


# Retries


@celery_app.task(bind=True, name="tests.flaky")
def flaky(self, error_name, failures):
    if self.request.retries < failures:
        raise {"connection": ConnectionError, "timeout": TimeoutError, "database": OperationalError}[error_name]()
    return self.request.retries


@celery_app.task(bind=True, name="tests.buggy")
def buggy(self, attempts):
    attempts.append(self.request.retries)
    raise ValueError("a bug, which another attempt won't fix")


@celery_app.task(name="tests.log_context")
def log_context():
    record = logging.LogRecord("tests", logging.INFO, __file__, 1, "message", None, None)
    TaskContextFilter().filter(record)
    return record.task_context


@pytest.fixture
def retry_inline(settings):
    """Eager propagation would raise Celery's internal Retry instead of running the next attempt."""
    settings.CELERY_TASK_EAGER_PROPAGATES = False


@pytest.mark.parametrize("error_name", ["connection", "timeout", "database"])
def test_transient_failures_are_retried_until_they_pass(retry_inline, error_name):
    assert flaky.apply(args=[error_name, 2]).get() == 2


def test_retries_are_bounded(retry_inline):
    with pytest.raises(ConnectionError):
        flaky.apply(args=["connection", Task.max_retries + 1]).get()


def test_the_last_retry_can_still_succeed(retry_inline):
    assert flaky.apply(args=["connection", Task.max_retries]).get() == Task.max_retries


def test_bugs_are_not_retried():
    attempts = []
    with pytest.raises(ValueError, match="a bug"):
        buggy.apply(args=[attempts]).get()
    assert attempts == [0]


def test_tasks_back_off_with_jitter():
    assert flaky.retry_backoff is True
    assert flaky.retry_jitter is True


# Logging


def test_log_lines_inside_a_task_name_it():
    context = log_context.apply(task_id="abc-123").get()
    assert context == " task=tests.log_context task_id=abc-123"


def test_log_lines_outside_a_task_stay_as_they_were():
    record = logging.LogRecord("tests", logging.INFO, __file__, 1, "message", None, None)
    TaskContextFilter().filter(record)
    assert record.task_context == ""


def test_task_log_context_never_includes_arguments():
    record = logging.LogRecord("tests", logging.INFO, __file__, 1, "message", None, None)

    @celery_app.task(name="tests.sensitive")
    def sensitive(email):
        TaskContextFilter().filter(record)

    sensitive.apply(args=["person@example.com"]).get()
    assert "example.com" not in record.task_context


def test_workers_log_through_django_settings():
    assert setup_logging.receivers
    assert "task_context" in settings.LOGGING["handlers"]["console"]["filters"]
