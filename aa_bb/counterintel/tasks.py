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
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .ingest.eve_activity import EveActivityIngestor
from .ingest.mumble import MumbleLogIngestor, MumblePresenceIngestor
from .ingest.paps import FCRosterIngestor
from .ingest.zkill import ZKillIngestor
from .models import CIEvent, CIOperation
from .services.agent import CIAgentService
from .services.correlation import CorrelationEngine
from .services.operations import CIOperationService

logger = logging.getLogger(__name__)

# Give zKillboard time to receive killmails after an operation's window closes
ZKILL_SCAN_DELAY_MINUTES = 30

PIPELINE_LOCK_KEY = "ci:pipeline_lock"
# Just under the pipeline's time limit, so a killed worker cannot hold it forever
PIPELINE_LOCK_SECONDS = 3300

# AFAT saves a Fat row per pilot; coalesce those saves into one sync per fleet
AFAT_SYNC_DEBOUNCE_SECONDS = 30


def _afat_sync_cache_key(source: str, afat_id: int) -> str:
    return f"ci:afat_sync:{source}:{afat_id}"


def queue_afat_sync(source: str, afat_id: int) -> bool:
    """
    Queue a debounced sync of an AFAT FatLink ("fatlink") or AFATFleet ("fleet").
    Returns False when a sync for it is already pending.
    """
    if not cache.add(
        _afat_sync_cache_key(source, afat_id), 1, timeout=AFAT_SYNC_DEBOUNCE_SECONDS * 4
    ):
        return False
    task = sync_afat_fatlink_task if source == "fatlink" else sync_afat_fleet_task
    task.apply_async(args=[afat_id], countdown=AFAT_SYNC_DEBOUNCE_SECONDS)
    return True


@shared_task(time_limit=600)
def sync_afat_fatlink_task(fatlink_id: int):
    """Sync one AFAT FatLink into its CIOperation and re-correlate it."""
    # Saves after this point queue another run
    cache.delete(_afat_sync_cache_key("fatlink", fatlink_id))
    try:
        # Third Party
        from afat.models import FatLink
    except ImportError:
        return None

    fatlink = FatLink.objects.filter(id=fatlink_id).first()
    if not fatlink:
        return None
    try:
        op = FCRosterIngestor.sync_fatlink(fatlink)
        CorrelationEngine.correlate_operation(op)
    except Exception:
        logger.exception(f"Failed to sync AFAT FatLink #{fatlink_id}")
        return None
    return op.id


@shared_task(time_limit=600)
def sync_afat_fleet_task(fleet_id: int):
    """Sync one AFATFleet into its CIOperation and re-correlate it."""
    cache.delete(_afat_sync_cache_key("fleet", fleet_id))
    try:
        # Third Party
        from afat.models import AFATFleet
    except ImportError:
        return None

    fleet = AFATFleet.objects.filter(id=fleet_id).first()
    if not fleet:
        return None
    try:
        op = FCRosterIngestor.sync_afat_fleet(fleet)
        CorrelationEngine.correlate_operation(op)
    except Exception:
        logger.exception(f"Failed to sync AFAT Fleet #{fleet_id}")
        return None
    return op.id


@shared_task(time_limit=1800)
def sync_recent_afat_fleets_task(hours: int = 48, correlate: bool = True):
    """
    Periodic job syncing recent AFAT and ESI fleet rosters directly into CIOperations
    and optionally triggering correlation calculations.
    """
    synced_ops = FCRosterIngestor.sync_recent_afat_fleets(hours=hours)
    for op in synced_ops if correlate else []:
        try:
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
    # A slow run (zKill rate limiting) must not overlap the next scheduled one
    if not cache.add(PIPELINE_LOCK_KEY, 1, timeout=PIPELINE_LOCK_SECONDS):
        logger.info("Counterintel pipeline already running; skipping this run.")
        return None
    try:
        afat_result = sync_recent_afat_fleets_task(hours=hours, correlate=False)
        eve_activity_count = EveActivityIngestor.ingest_latest_activity()
        mumble_result = ingest_mumble_logs_task(correlate=False)
        zkill_scanned_ops = scan_concluded_operations_combat(hours=hours)
        correlation_result = run_scheduled_ci_correlation(hours=hours)
    finally:
        cache.delete(PIPELINE_LOCK_KEY)
    return {
        "afat": afat_result,
        "eve_activity_events": eve_activity_count,
        "mumble": mumble_result,
        "zkill_scanned_operations": zkill_scanned_ops,
        "correlation": correlation_result,
    }


@shared_task(time_limit=1800)
def scan_concluded_operations_combat(hours: int = 48, limit: int = 5) -> list[int]:
    """
    Scan zKillboard for recent operations whose operational window closed at least
    ZKILL_SCAN_DELAY_MINUTES ago and have not been scanned yet.
    Returns the IDs of operations that were scanned.
    """
    now = timezone.now()
    ready_before = now - timedelta(minutes=ZKILL_SCAN_DELAY_MINUTES)
    candidates = CIOperation.objects.filter(
        start_time__gte=now - timedelta(hours=hours),
        combat_scanned_at__isnull=True,
    ).order_by("start_time")

    scanned = []
    for op in candidates:
        if len(scanned) >= limit:
            break
        _window_start, window_end = CIOperationService.get_operational_window(op)
        if window_end > ready_before:
            continue
        try:
            ZKillIngestor.scan_operation(op)
        except Exception:
            logger.exception(f"zKill scan failed for operation {op.id}")
            continue
        scanned.append(op.id)
    return scanned


@shared_task(time_limit=1800)
def rescan_operation_combat_task(operation_id: int) -> int:
    """
    Re-scan zKillboard for a single operation and re-correlate it, e.g. after a
    CI manager edited the operation's window.
    """
    op = CIOperation.objects.filter(id=operation_id).first()
    if not op:
        logger.warning(f"CIOperation {operation_id} not found for combat rescan.")
        return 0
    try:
        events = ZKillIngestor.scan_operation(op)
    except Exception:
        logger.exception(f"zKill scan failed for operation {op.id}")
        return 0
    CorrelationEngine.correlate_operation(op)
    return len(events)


@shared_task(time_limit=1800)
def recompute_leak_exposure_task(operation_id: int | None = None) -> int:
    """
    Re-correlate every leaked operation in the leak lookback, since marking or
    clearing one leak changes every member's leaked-vs-clean exposure. The given
    operation is always re-correlated so a cleared leak drops its findings.
    """
    since = timezone.now() - timedelta(days=CorrelationEngine.LEAK_LOOKBACK_DAYS)
    ops = CIOperation.objects.filter(Q(leaked=True, start_time__gte=since))
    if operation_id:
        ops = ops | CIOperation.objects.filter(id=operation_id)
    processed = 0
    for op in ops.distinct():
        try:
            CorrelationEngine.correlate_operation(op)
        except Exception:
            logger.exception(f"Leak exposure recompute failed for operation {op.id}")
            continue
        processed += 1
    return processed


@shared_task(time_limit=1800)
def ingest_mumble_logs_task(correlate: bool = True):
    """
    Pull Mumble authentications from the AA authenticator log, poll Alliance Auth's
    MumbleUser connect/disconnect timestamps, rebuild session intervals, and
    optionally run operational correlation.
    """
    candidate_paths = [
        path
        for path in (getattr(settings, "MUMBLE_AUTHENTICATOR_LOG_PATH", None),)
        if path
    ]

    found_paths = [p for p in candidate_paths if os.path.exists(p)]
    logger.info(
        "Mumble ingestion candidates=%s; readable existing paths=%s",
        candidate_paths,
        found_paths,
    )
    target_paths = found_paths

    total_events = 0
    for path in target_paths:
        events = MumbleLogIngestor.ingest_log_file(path)
        total_events += len(events)
        if events:
            logger.info(f"Ingested {len(events)} Mumble events from {path}.")
        elif os.path.exists(path):
            try:
                file_size = os.path.getsize(path)
            except OSError:
                file_size = "unknown"
            logger.warning(
                "Mumble log produced no events: path=%s size=%s. "
                "The file may be empty, already consumed by its ingest offset, "
                "or contain a format not recognized by the parser.",
                path,
                file_size,
            )

    if candidate_paths and not found_paths:
        logger.error(
            "Configured Mumble authenticator log does not exist on this Celery worker. "
            "Verify the path is mounted/readable by the worker process: %s",
            candidate_paths,
        )

    presence_events = MumblePresenceIngestor.poll()
    if presence_events:
        logger.info(
            f"Ingested {len(presence_events)} Mumble presence events from MumbleUser."
        )

    if correlate:
        run_scheduled_ci_correlation.delay()
    return (
        f"Ingested {total_events} log events from {target_paths} and "
        f"{len(presence_events)} presence events."
    )


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
    triaged = 0
    for op in recent_ops:
        try:
            if op.is_concluded:
                # Correlates and opens review cases for flagged members
                triaged += len(CIAgentService.auto_triage_operation(op))
            else:
                CorrelationEngine.correlate_operation(op)
        except Exception:
            logger.exception(f"CI correlation failed for operation {op.id}")
            continue
        processed += 1

    return (
        f"Processed CI correlation for {processed} operations; "
        f"{triaged} investigation case(s) triaged."
    )


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
