"""
Celery tasks for counterintelligence correlation and historical replay.
Routed to dedicated queue 'aa_bb_ci' to ensure character audits remain unaffected.
"""

# Standard Library
import logging
from datetime import timedelta

# Third Party
from celery import shared_task

# Django
from django.conf import settings
from django.utils import timezone

from .ingest.mumble import MumbleLogIngestor
from .ingest.paps import FCRosterIngestor
from .models import CIOperation
from .services.correlation import CorrelationEngine

logger = logging.getLogger(__name__)


@shared_task(queue="aa_bb_ci", time_limit=1800)
def sync_recent_afat_fleets_task(hours: int = 48):
    """
    Periodic job syncing recent AFAT and ESI fleet rosters directly into CIOperations
    and triggering automated correlation calculations.
    """
    synced_ops = FCRosterIngestor.sync_recent_afat_fleets(hours=hours)
    for op in synced_ops:
        try:
            CorrelationEngine.correlate_operation(op)
        except Exception as e:
            logger.error(f"Error auto-correlating synced AFAT operation {op.id}: {e}")

    logger.info(f"Synced {len(synced_ops)} AFAT fleets to Counterintelligence.")
    return f"Synced {len(synced_ops)} AFAT fleets."


@shared_task(queue="aa_bb_ci", time_limit=1800)
def ingest_mumble_logs_task(log_file_path: str = None):
    """
    Periodic or on-demand task pulling Mumble authenticator/server logs from disk,
    updating session intervals, and running operational correlation.
    """
    path = (
        log_file_path
        or getattr(settings, "MUMBLE_AUTHENTICATOR_LOG_PATH", None)
        or getattr(settings, "MUMBLE_LOG_PATH", None)
        or "/var/log/mumble-authenticator.log"
    )
    events = MumbleLogIngestor.ingest_log_file(path)
    logger.info(f"Ingested {len(events)} Mumble events from {path}.")

    # Run correlation on recent operations
    (
        run_scheduled_ci_correlation.delay()
        if hasattr(run_scheduled_ci_correlation, "delay")
        else run_scheduled_ci_correlation()
    )
    return f"Ingested {len(events)} events from {path}."


@shared_task(queue="aa_bb_ci", time_limit=1800)
def correlate_operation_task(operation_id: int, algorithm_version: str = "1.0.0"):
    """
    Run correlation engine against a single operation.
    """
    op = CIOperation.objects.filter(id=operation_id).first()
    if not op:
        logger.warning(f"CIOperation {operation_id} not found for correlation task.")
        return 0

    findings = CorrelationEngine.correlate_operation(
        op, algorithm_version=algorithm_version
    )
    logger.info(f"CI Correlation complete for {op.name} ({len(findings)} findings).")
    return len(findings)


@shared_task(queue="aa_bb_ci", time_limit=3600)
def replay_historical_operation_task(
    operation_id: int, algorithm_version: str = "1.0.0"
):
    """
    Deterministic historical replay of an operation.
    """
    op = CIOperation.objects.filter(id=operation_id).first()
    if not op:
        logger.warning(f"CIOperation {operation_id} not found for historical replay.")
        return 0

    findings = CorrelationEngine.correlate_operation(
        op, algorithm_version=algorithm_version
    )
    return {
        "operation_id": op.id,
        "operation_name": op.name,
        "algorithm_version": algorithm_version,
        "findings_count": len(findings),
        "replayed_at": timezone.now().isoformat(),
    }


@shared_task(queue="aa_bb_ci", time_limit=1800)
def run_scheduled_ci_correlation():
    """
    Periodic job checking recent operations within the last 48 hours and triggering correlation.
    """
    cutoff = timezone.now() - timedelta(hours=48)
    recent_ops = CIOperation.objects.filter(start_time__gte=cutoff)
    processed = 0
    for op in recent_ops:
        CorrelationEngine.correlate_operation(op)
        processed += 1

    return f"Processed CI correlation for {processed} operations."
