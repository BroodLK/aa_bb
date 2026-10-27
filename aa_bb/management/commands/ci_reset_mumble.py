"""Wipe Counterintel Mumble telemetry and re-ingest it with the current parser."""

# Django
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

# AA BigBrother
from aa_bb.counterintel.models import (
    CICorrelation,
    CIEvent,
    CIInvestigation,
    CILogIngestState,
    CISession,
)
from aa_bb.counterintel.tasks import (
    ingest_mumble_logs_task,
    run_scheduled_ci_correlation,
)

# Findings derived from comms sessions; rebuilt by the re-correlation below
COMMS_ANOMALY_TYPES = (
    "comms_without_pap",
    "pre_contact_disconnect",
    "briefing_without_deployment",
    "co_occurring_disconnect_ring",
    "alt_shared_mumble_identity",
)


class Command(BaseCommand):
    help = (
        "Delete all Mumble CIEvents/CISessions and comms-derived correlations, reset "
        "the authenticator log offset, then re-ingest and re-correlate."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=int,
            default=24 * 30,
            help="Re-correlate operations that started within this many hours.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only report what would be deleted.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt.",
        )

    def handle(self, *args, **options):
        log_paths = [
            path
            for path in (getattr(settings, "MUMBLE_AUTHENTICATOR_LOG_PATH", None),)
            if path
        ]

        events = CIEvent.objects.filter(source="mumble")
        sessions = CISession.objects.filter(source="mumble")
        ingest_states = CILogIngestState.objects.filter(file_path__in=log_paths)
        investigated = set(
            CIInvestigation.objects.exclude(operation__isnull=True).values_list(
                "subject_user_id", "operation_id"
            )
        )
        correlation_ids = [
            corr_id
            for corr_id, user_id, op_id in CICorrelation.objects.filter(
                anomaly_type__in=COMMS_ANOMALY_TYPES
            ).values_list("id", "user_id", "operation_id")
            if (user_id, op_id) not in investigated
        ]

        self.stdout.write(
            f"Mumble events: {events.count()}\n"
            f"Mumble sessions: {sessions.count()}\n"
            f"Comms-derived correlations (without an investigation): "
            f"{len(correlation_ids)}\n"
            f"Log ingest offsets to reset: {ingest_states.count()} ({log_paths})"
        )

        if options["dry_run"]:
            return

        if not options["yes"]:
            answer = input("Delete and re-ingest? [y/N] ").strip().lower()
            if answer != "y":
                raise CommandError("Aborted.")

        with transaction.atomic():
            events.delete()
            sessions.delete()
            CICorrelation.objects.filter(id__in=correlation_ids).delete()
            ingest_states.delete()

        self.stdout.write(ingest_mumble_logs_task(correlate=False))
        self.stdout.write(run_scheduled_ci_correlation(hours=options["hours"]))
        self.stdout.write(self.style.SUCCESS("Mumble telemetry rebuilt."))
