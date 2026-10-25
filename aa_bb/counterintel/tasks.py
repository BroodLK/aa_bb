"""
Celery tasks for counterintelligence correlation and historical replay.
"""

# Standard Library
import logging
import os
from datetime import timedelta

# Third Party
from celery import shared_task

# Django
from django.conf import settings
from django.utils import timezone

from .ingest.eve_activity import EveActivityIngestor
from .ingest.mumble import MumbleLogIngestor
from .ingest.paps import FCRosterIngestor
from .ingest.zkill import ZKillIngestor
from .models import CIEvent, CIOperation
from .services.correlation import CorrelationEngine

logger = logging.getLogger(__name__)


@shared_task(time_limit=1800)
def sync_recent_afat_fleets_task(hours: int = 48):
    """
    Periodic job syncing recent AFAT and ESI fleet rosters directly into CIOperations,
    running completed operation zKill combat enrichments, and triggering correlation calculations.
    """
    synced_ops = FCRosterIngestor.sync_recent_afat_fleets(hours=hours)
    for op in synced_ops:
        try:
            # Only run zKill analysis if operation has concluded
            if op.end_time:
                ZKillIngestor.analyze_completed_operation(op)
            CorrelationEngine.correlate_operation(op)
        except Exception as e:
            logger.error(f"Error auto-correlating synced AFAT operation {op.id}: {e}")

    logger.info(f"Synced {len(synced_ops)} AFAT fleets to Counterintelligence.")
    return f"Synced {len(synced_ops)} AFAT fleets."


@shared_task(time_limit=3600)
def run_counterintel_pipeline_task(hours: int = 48):
    """Refresh live telemetry sources and rebuild recent correlations.

    Discord events are written by the Discord bot as they happen. This task
    consumes those normalized events after refreshing AFAT and Mumble, then
    correlates the complete recent window.
    """
    afat_result = sync_recent_afat_fleets_task(hours=hours)
    eve_activity_count = EveActivityIngestor.ingest_latest_logoffs()
    mumble_result = ingest_mumble_logs_task()
    correlation_result = run_scheduled_ci_correlation(hours=hours)
    return {
        "afat": afat_result,
        "eve_activity_events": eve_activity_count,
        "mumble": mumble_result,
        "correlation": correlation_result,
    }


@shared_task(time_limit=1800)
def ingest_mumble_logs_task(log_file_path: str = None):
    """
    Periodic or on-demand task pulling Mumble authenticator/server logs from disk,
    updating session intervals, and running operational correlation.
    """
    candidate_paths = []
    if log_file_path:
        candidate_paths.append(log_file_path)

    # Configured settings
    for setting_key in [
        "MUMBLE_AUTHENTICATOR_LOG_PATH",
        "MUMBLE_LOG_PATH",
        "MUMBLE_LOG_FILE",
    ]:
        val = getattr(settings, setting_key, None)
        if val and val not in candidate_paths:
            candidate_paths.append(val)

    # Standard Alliance Auth installation paths
    base_dir = getattr(settings, "BASE_DIR", "")
    if base_dir:
        for fname in ["mumble_authenticator.log", "mumble.log", "authenticator.log"]:
            p = os.path.join(base_dir, "log", fname)
            if p not in candidate_paths:
                candidate_paths.append(p)

    # Common system / server paths
    system_paths = [
        "/home/allianceserver/myauth/log/mumble_authenticator.log",
        "/home/allianceserver/myauth/log/mumble.log",
        "/var/log/mumble_authenticator.log",
        "/var/log/mumble-authenticator.log",
        "/var/log/mumble/mumble-server.log",
        "/var/log/murmur/mumble-server.log",
        "/var/log/mumble-server.log",
    ]
    for sp in system_paths:
        if sp not in candidate_paths:
            candidate_paths.append(sp)

    found_paths = [p for p in candidate_paths if os.path.exists(p)]
    target_paths = (
        found_paths
        if found_paths
        else (
            [candidate_paths[0]]
            if candidate_paths
            else ["/home/allianceserver/myauth/log/mumble_authenticator.log"]
        )
    )

    total_events = 0
    for path in target_paths:
        events = MumbleLogIngestor.ingest_log_file(path)
        total_events += len(events)
        if events:
            logger.info(f"Ingested {len(events)} Mumble events from {path}.")

    # Run correlation on recent operations
    (
        run_scheduled_ci_correlation.delay()
        if hasattr(run_scheduled_ci_correlation, "delay")
        else run_scheduled_ci_correlation()
    )
    return f"Ingested {total_events} events from {target_paths}."


@shared_task(time_limit=1800)
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


@shared_task(time_limit=3600)
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


@shared_task(time_limit=1800)
def run_scheduled_ci_correlation(hours: int = 48):
    """
    Periodic job checking recent operations within the last 48 hours and triggering correlation.
    """
    cutoff = timezone.now() - timedelta(hours=hours)
    recent_ops = CIOperation.objects.filter(start_time__gte=cutoff)
    processed = 0
    for op in recent_ops:
        CorrelationEngine.correlate_operation(op)
        processed += 1

    return f"Processed CI correlation for {processed} operations."


@shared_task(time_limit=1800)
def prune_ci_telemetry_task(retention_days: int = 90) -> str:
    """
    RAM-conscious automated data retention task.
    Prunes raw second-by-second CIEvent records older than `retention_days` in batches
    to avoid memory spikes and table locking, while preserving CISession, CIOperation,
    CICorrelation, and CIInvestigation records indefinitely.
    """
    cutoff = timezone.now() - timedelta(days=retention_days)
    total_deleted = 0
    batch_size = 1000

    while True:
        # Fetch batch of IDs to delete in chunks
        batch_ids = list(
            CIEvent.objects.filter(timestamp__lt=cutoff).values_list("id", flat=True)[
                :batch_size
            ]
        )
        if not batch_ids:
            break

        deleted_count, _ = CIEvent.objects.filter(id__in=batch_ids).delete()
        total_deleted += deleted_count
        if len(batch_ids) < batch_size:
            break

    logger.info(
        f"Pruned {total_deleted} raw CIEvent records older than {retention_days} days."
    )
    return f"Pruned {total_deleted} raw telemetry events."
