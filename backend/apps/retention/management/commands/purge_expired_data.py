from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.retention import purge


class Command(BaseCommand):
    help = "Apply the data-retention policy. Idempotent; intended to run daily (Celery beat in Phase 2.5)."

    def handle(self, *args, **options):
        for name, count in purge.run(timezone.now()).items():
            self.stdout.write(f"{name}: {count}")
