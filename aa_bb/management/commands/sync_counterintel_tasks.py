"""Synchronize Counterintel tasks into django-celery-beat."""

# Third Party
from django_celery_beat.models import CrontabSchedule, PeriodicTask

# Django
from django.core.management.base import BaseCommand
from django.db import transaction

COUNTERINTEL_TASK_PREFIX = "CI:"


TASKS = (
    {
        "name": "CI: Refresh And Correlate Live Telemetry",
        "task": "aa_bb.counterintel.tasks.run_counterintel_pipeline_task",
        "minute": "*/5",
        "enabled": True,
    },
    {
        "name": "CI: Sync Recent AFAT Fleets",
        "task": "aa_bb.counterintel.tasks.sync_recent_afat_fleets_task",
        "minute": "*/5",
        "enabled": False,
    },
    {
        "name": "CI: Ingest Mumble Logs",
        "task": "aa_bb.counterintel.tasks.ingest_mumble_logs_task",
        "minute": "*/2",
        "enabled": False,
    },
    {
        "name": "CI: Correlate Recent Operations",
        "task": "aa_bb.counterintel.tasks.run_scheduled_ci_correlation",
        "minute": "*/5",
        "enabled": False,
    },
    {
        "name": "CI: Prune Telemetry",
        "task": "aa_bb.counterintel.tasks.prune_ci_telemetry_task",
        "minute": "0",
        "hour": "3",
        "enabled": False,
    },
)


class Command(BaseCommand):
    help = "Create or overwrite Counterintel django-celery-beat tasks."

    @transaction.atomic
    def handle(self, *args, **options):
        managed_names = {spec["name"] for spec in TASKS}
        stale_tasks = PeriodicTask.objects.filter(
            name__startswith=COUNTERINTEL_TASK_PREFIX
        ).exclude(name__in=managed_names)
        stale_count = stale_tasks.count()
        stale_tasks.delete()

        for spec in TASKS:
            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute=spec.get("minute", "*"),
                hour=spec.get("hour", "*"),
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )
            PeriodicTask.objects.update_or_create(
                name=spec["name"],
                defaults={
                    "task": spec["task"],
                    "crontab": schedule,
                    "interval": None,
                    "solar": None,
                    "clocked": None,
                    "enabled": spec["enabled"],
                    "kwargs": "{}",
                    "args": "[]",
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Synchronized {len(TASKS)} Counterintel tasks; "
                f"removed {stale_count} stale task(s)."
            )
        )
