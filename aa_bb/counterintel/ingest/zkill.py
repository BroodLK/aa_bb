"""
zKillboard combat event enrichment with rate limiting and deduplication.
"""

# Standard Library
import logging
import math
import time
from datetime import datetime

# Django
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import CIEvent, CIOperation
from ..services.identity import IdentityResolver

logger = logging.getLogger(__name__)


class ZKillIngestor:
    """
    Ingests and normalizes combat events from zKillboard into CIEvents
    associated with operations and users.
    """

    CACHE_TTL = 3600  # 1 hour cache

    ZKILL_CHARACTER_URL = (
        "https://zkillboard.com/api/characterID/{character_id}/pastSeconds/{seconds}/"
    )
    # zKillboard only serves pastSeconds up to 7 days, in whole hours
    ZKILL_MAX_PAST_SECONDS = 7 * 24 * 3600
    # How long to wait for a free slot in the shared zKill rate limiter per request
    ZKILL_SLOT_WAIT_SECONDS = 30

    @staticmethod
    def parse_killmail_time(killmail_data: dict):
        raw_ts = killmail_data.get("killmail_time") or killmail_data.get("killTime")
        if isinstance(raw_ts, str):
            ts = parse_datetime(raw_ts)
        elif isinstance(raw_ts, datetime):
            ts = raw_ts
        else:
            return None
        if ts and timezone.is_naive(ts):
            ts = timezone.make_aware(ts)
        return ts

    @classmethod
    def ingest_killmail(
        cls,
        killmail_data: dict,
        operation: CIOperation = None,
    ) -> CIEvent:
        """
        Normalize a single killmail payload into a CIEvent.
        """
        killmail_id = str(
            killmail_data.get("killmail_id") or killmail_data.get("killID") or ""
        )
        ts = cls.parse_killmail_time(killmail_data) or timezone.now()

        victim = killmail_data.get("victim") or {}
        victim_char_id = victim.get("character_id")
        attackers = killmail_data.get("attackers") or []

        attacker_char_ids = [
            att.get("character_id") for att in attackers if att.get("character_id")
        ]

        # Resolve primary subject user if any participant belongs to the alliance
        resolved_user = None
        if victim_char_id:
            resolved_user = IdentityResolver.resolve_user_by_character(
                victim_char_id, at_time=ts
            )

        if not resolved_user:
            for att_id in attacker_char_ids:
                u = IdentityResolver.resolve_user_by_character(att_id, at_time=ts)
                if u:
                    resolved_user = u
                    break

        source_event_id = f"zkill:{killmail_id}"
        payload = {
            "killmail_id": killmail_id,
            "solar_system_id": killmail_data.get("solar_system_id"),
            "victim_character_id": victim_char_id,
            "victim_ship_type_id": victim.get("ship_type_id"),
            "victim_corporation_id": victim.get("corporation_id"),
            "victim_alliance_id": victim.get("alliance_id"),
            "attacker_count": len(attackers),
            "attacker_character_ids": attacker_char_ids,
            "zkb": killmail_data.get("zkb", {}),
        }
        dedup_hash = CIEvent.generate_dedup_hash("zkill", source_event_id, ts, payload)

        event, _ = CIEvent.objects.get_or_create(
            dedup_hash=dedup_hash,
            defaults={
                "source": "zkill",
                "source_event_id": source_event_id,
                "timestamp": ts,
                "user": resolved_user,
                "operation": operation,
                "payload": payload,
            },
        )
        return event

    @classmethod
    def ingest_killmails_batch(
        cls,
        killmails: list[dict],
        operation: CIOperation = None,
    ) -> list[CIEvent]:
        events = []
        for km in killmails:
            ev = cls.ingest_killmail(km, operation=operation)
            events.append(ev)
        return events

    @classmethod
    def analyze_completed_operation(
        cls,
        operation: CIOperation,
        killmails: list[dict] = None,
    ) -> list[CIEvent]:
        """
        Enforces that zKillboard combat analysis is strictly executed only AFTER
        an operation has concluded (completed operation pulled from AFAT/ESI).
        """
        if not operation.end_time:
            logger.info(
                f"Skipping zKill analysis for operation '{operation.name}' (ID: {operation.id}): "
                f"operation is active or ongoing."
            )
            return []

        # Check existing combat events for this operation
        existing_combat_count = CIEvent.objects.filter(
            operation=operation,
            source="zkill",
        ).count()

        if existing_combat_count > 0 and not killmails:
            logger.info(
                f"Operation '{operation.name}' already has {existing_combat_count} combat events recorded."
            )
            return list(CIEvent.objects.filter(operation=operation, source="zkill"))

        if killmails:
            return cls.ingest_killmails_batch(killmails, operation=operation)

        return list(CIEvent.objects.filter(operation=operation, source="zkill"))

    @classmethod
    def scan_operation(cls, operation: CIOperation) -> list[CIEvent]:
        """
        Pull every killmail involving the operation's FC-confirmed characters during
        its operational window from zKillboard, hydrate them from ESI and store them.

        ``combat_scanned_at`` is only set once every character and killmail was
        fetched, so a scan cut short by rate limits or outages is retried later.
        """
        # AA BigBrother
        from aa_bb.checks import awox

        from ..services.operations import CIOperationService

        window_start, window_end = CIOperationService.get_operational_window(operation)
        now = timezone.now()
        seconds_back = (now - window_start).total_seconds()
        if seconds_back > cls.ZKILL_MAX_PAST_SECONDS:
            logger.warning(
                f"Operation '{operation.name}' (ID: {operation.id}) started more than "
                f"7 days ago; zKillboard cannot be scanned for its window."
            )
            cls._mark_scanned(operation, now)
            return []
        past_seconds = min(
            cls.ZKILL_MAX_PAST_SECONDS, math.ceil(seconds_back / 3600) * 3600
        )

        character_ids = sorted(
            set(operation.participants.values_list("character_id", flat=True))
        )
        session = awox._get_requests_session()
        complete = True

        zkb_by_killmail = {}
        for character_id in character_ids:
            entries = cls._fetch_character_killmails(
                awox, session, character_id, past_seconds
            )
            if entries is None:
                complete = False
                continue
            for entry in entries:
                killmail_id = entry.get("killmail_id")
                zkb = entry.get("zkb") or {}
                if killmail_id and zkb.get("hash"):
                    zkb_by_killmail.setdefault(killmail_id, zkb)

        events = []
        for killmail_id, zkb in zkb_by_killmail.items():
            existing = CIEvent.objects.filter(
                source="zkill", source_event_id=f"zkill:{killmail_id}"
            ).first()
            if existing:
                if window_start <= existing.timestamp <= window_end:
                    events.append(existing)
                continue

            killmail = cls._fetch_esi_killmail(killmail_id, zkb["hash"])
            if not killmail:
                complete = False
                continue
            ts = cls.parse_killmail_time(killmail)
            if not ts or not window_start <= ts <= window_end:
                continue
            events.append(
                cls.ingest_killmail({**killmail, "zkb": zkb}, operation=operation)
            )

        if complete:
            cls._mark_scanned(operation, now)
        else:
            logger.warning(
                f"zKill scan for operation '{operation.name}' (ID: {operation.id}) "
                f"incomplete; it will be retried on the next pipeline run."
            )
        logger.info(
            f"zKill scan for operation '{operation.name}' (ID: {operation.id}): "
            f"{len(character_ids)} characters, {len(events)} killmails in window."
        )
        return events

    @classmethod
    def _fetch_character_killmails(
        cls, awox, session, character_id: int, past_seconds: int
    ) -> list[dict] | None:
        """zKill entries for a character, or None if the request could not be made."""
        deadline = time.monotonic() + cls.ZKILL_SLOT_WAIT_SECONDS
        while not awox._try_acquire_zkill_slot():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

        url = cls.ZKILL_CHARACTER_URL.format(
            character_id=character_id, seconds=past_seconds
        )
        try:
            response = session.get(url, timeout=(5, 15))
            response.raise_for_status()
        except Exception as e:
            logger.warning(f"zKill request failed for character {character_id}: {e}")
            return None

        content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            awox._notify_zkill_down_once(
                response.text[:200], response.status_code, content_type
            )
            return None

        data = response.json()
        return data if isinstance(data, list) else []

    @classmethod
    def _fetch_esi_killmail(cls, killmail_id: int, killmail_hash: str) -> dict | None:
        # AA BigBrother
        from aa_bb.app_settings import DATASOURCE, esi_tenant_kwargs
        from aa_bb.providers import ESIHandler

        return ESIHandler.get_killmails_killmail_id_killmail_hash(
            killmail_id=killmail_id,
            killmail_hash=killmail_hash,
            operation_kwargs=esi_tenant_kwargs(DATASOURCE),
            allow_not_modified=True,
            swallow_errors=True,
        ) or ESIHandler.get_killmails_killmail_id_killmail_hash(
            killmail_id=killmail_id,
            killmail_hash=killmail_hash,
            operation_kwargs=esi_tenant_kwargs(DATASOURCE),
            use_etag=False,
            swallow_errors=True,
        )

    @staticmethod
    def _mark_scanned(operation: CIOperation, scanned_at):
        operation.combat_scanned_at = scanned_at
        operation.save(update_fields=["combat_scanned_at"])
