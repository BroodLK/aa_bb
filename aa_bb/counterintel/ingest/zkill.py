"""
zKillboard combat event enrichment with rate limiting and deduplication.
"""

# Standard Library
import logging
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
        raw_ts = killmail_data.get("killmail_time") or killmail_data.get("killTime")
        if isinstance(raw_ts, str):
            ts = parse_datetime(raw_ts)
        elif isinstance(raw_ts, datetime):
            ts = raw_ts
        else:
            ts = timezone.now()

        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)

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
