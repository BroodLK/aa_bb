"""
Read-only FC-submitted PAP / AFAT fleet roster ingestion engine.
Strictly authoritative FC rosters with zero member participation or self-reporting.
"""

# Standard Library
import hashlib
import re
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from ..models import CIEvent, CIOperation, CIOperationParticipant, CISession
from ..services.identity import IdentityResolver


class FCRosterIngestor:
    """
    Ingests FC-submitted fleet rosters into confirmed CIOperationParticipant records
    and normalized PAP CIEvents.
    """

    @classmethod
    def ingest_fc_roster(
        cls,
        operation: CIOperation,
        roster_entries: list,  # list of character_id (int) or char_name (str) or dict {character_id, character_name, ship_type, etc.}
        fc_user: User = None,
        recorded_at=None,
    ) -> list[CIOperationParticipant]:
        """
        Process an FC-submitted roster. Members are never prompted or involved.
        """
        ts = recorded_at or timezone.now()
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)

        participants = []
        seen_keys = set()
        deduped_entries = []

        # Deduplicate incoming roster entries preserving order
        for entry in roster_entries:
            key = None
            if isinstance(entry, int) or (isinstance(entry, str) and entry.isdigit()):
                key = ("id", int(entry))
            elif isinstance(entry, str):
                key = ("str", entry.strip().lower())
            elif isinstance(entry, dict):
                key = ("dict", entry.get("character_id") or (entry.get("character_name") or "").lower())
            else:
                key = ("other", str(entry))

            if key not in seen_keys:
                seen_keys.add(key)
                deduped_entries.append(entry)

        for entry in deduped_entries:
            char_id = None
            char_name = None

            if isinstance(entry, int) or (isinstance(entry, str) and entry.isdigit()):
                char_id = int(entry)
            elif isinstance(entry, str):
                char_name = entry.strip()
            elif isinstance(entry, dict):
                char_id = entry.get("character_id")
                char_name = entry.get("character_name")

            resolved_user = None
            if char_id:
                resolved_user = IdentityResolver.resolve_user_by_character(
                    char_id, at_time=ts
                )

            if not resolved_user and char_name:
                try:
                    # Alliance Auth
                    from allianceauth.eveonline.models import EveCharacter

                    ec = EveCharacter.objects.filter(
                        character_name__iexact=char_name
                    ).first()
                    if ec:
                        char_id = ec.character_id
                        resolved_user = IdentityResolver.resolve_user_by_character(
                            char_id, at_time=ts
                        )
                except Exception:
                    pass

            if not resolved_user and char_name:
                # Fallback to direct username match
                u = User.objects.filter(username__iexact=char_name).first()
                if u:
                    resolved_user = u
                    if not char_id:
                        profile = getattr(u, "profile", None)
                        if profile and profile.main_character:
                            char_id = profile.main_character.character_id

            if not char_id:
                # Deterministic pseudo ID for tracking unresolvable names across restarts
                hash_input = (char_name or str(entry)).strip().lower().encode("utf-8")
                char_id = int(hashlib.sha256(hash_input).hexdigest()[:10], 16) % (2**31 - 1)

            if resolved_user:
                part, _ = CIOperationParticipant.objects.update_or_create(
                    operation=operation,
                    character_id=char_id,
                    defaults={
                        "user": resolved_user,
                        "confirmed_by_fc": True,
                        "recorded_at": ts,
                    },
                )
                if part not in participants:
                    participants.append(part)

            # Record idempotent CIEvent
            source_event_id = f"pap:{operation.id}:{char_id}:{ts.timestamp()}"
            payload = {
                "operation_id": operation.id,
                "character_id": char_id,
                "character_name": char_name,
                "fc_user_id": (
                    fc_user.id
                    if fc_user
                    else (
                        operation.fleet_commander_id
                        if operation.fleet_commander
                        else None
                    )
                ),
            }
            dedup_hash = CIEvent.generate_dedup_hash(
                "pap", source_event_id, ts, payload
            )

            CIEvent.objects.get_or_create(
                dedup_hash=dedup_hash,
                defaults={
                    "source": "pap",
                    "source_event_id": source_event_id,
                    "timestamp": ts,
                    "user": resolved_user,
                    "operation": operation,
                    "payload": payload,
                },
            )

        return participants

    @classmethod
    def ingest_roster_text(
        cls,
        operation: CIOperation,
        text_data: str,
        fc_user: User = None,
        recorded_at=None,
    ) -> list[CIOperationParticipant]:
        """
        Parse raw text pasted by an FC or officer (e.g. EVE fleet composition window,
        chat channel member list, or comma/newline separated character names).
        Requires zero plugin dependencies or member action.
        """
        if not text_data or not text_data.strip():
            return []

        entries = []
        seen = set()
        lines = text_data.strip().splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Strip chat timestamps like [ 2026.10.22 21:05:00 ] Character Name > ...
            chat_match = re.match(r"^\[.*?\]\s*([^:>]+?)(?:\s*>.*)?$", line)
            if chat_match:
                candidate = chat_match.group(1).strip()
                if candidate and candidate.lower() not in seen:
                    seen.add(candidate.lower())
                    entries.append(candidate)
                    continue

            # Check tab-separated or pipe-separated lines (e.g. Fleet Comp window)
            parts = re.split(r"[\t,|]", line)
            if parts:
                candidate = parts[0].strip()
                if candidate and candidate.lower() not in seen:
                    seen.add(candidate.lower())
                    entries.append(candidate)
                    continue

            if line.lower() not in seen:
                seen.add(line.lower())
                entries.append(line)

        return cls.ingest_fc_roster(
            operation=operation,
            roster_entries=entries,
            fc_user=fc_user,
            recorded_at=recorded_at,
        )

    @classmethod
    def auto_detect_fleet_participants(
        cls,
        operation: CIOperation,
        channel_keywords: list = None,
        min_duration_seconds: int = 30,
    ) -> list[CIOperationParticipant]:
        """
        Automatically detect and confirm fleet participants from operational
        communication presence (Mumble, Discord) during the operation window.
        Provides zero-overhead roster generation for alliances not using PAP/AFAT.
        """
        op_start = operation.start_time
        op_end = operation.end_time or (op_start + timedelta(hours=2))

        detected_users = set()

        # 1. Query CISession overlapping [op_start, op_end]
        sessions_qs = (
            CISession.objects.filter(
                start_time__lte=op_end,
            )
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=op_start))
            .select_related("user")
        )

        if channel_keywords:
            q_channel = Q()
            for kw in channel_keywords:
                if kw and str(kw).strip():
                    q_channel |= Q(channel_name__icontains=str(kw).strip())
            if q_channel:
                sessions_qs = sessions_qs.filter(q_channel)

        for s in sessions_qs:
            dur = s.duration_seconds
            if dur is None or dur >= min_duration_seconds:
                if s.user:
                    detected_users.add(s.user)

        # 2. Query CIEvent voice events during window [op_start, op_end]
        events_qs = (
            CIEvent.objects.filter(
                source__in=["mumble", "discord"],
                timestamp__gte=op_start,
                timestamp__lte=op_end,
                user__isnull=False,
            )
            .select_related("user")
        )
        if channel_keywords:
            valid_kws = [str(kw).strip().lower() for kw in channel_keywords if kw and str(kw).strip()]
            for ev in events_qs:
                payload = ev.payload or {}
                ch = str(payload.get("channel_name") or payload.get("channel") or "").lower()
                if valid_kws and any(kw in ch for kw in valid_kws):
                    detected_users.add(ev.user)
        else:
            for ev in events_qs:
                detected_users.add(ev.user)

        participants = []
        for user in detected_users:
            profile = getattr(user, "profile", None)
            char_id = None
            if profile and profile.main_character:
                char_id = profile.main_character.character_id
            else:
                hash_input = user.username.strip().lower().encode("utf-8")
                char_id = int(hashlib.sha256(hash_input).hexdigest()[:10], 16) % (2**31 - 1)

            part, _ = CIOperationParticipant.objects.update_or_create(
                operation=operation,
                character_id=char_id,
                defaults={
                    "user": user,
                    "confirmed_by_fc": True,
                    "recorded_at": op_start,
                },
            )
            if part not in participants:
                participants.append(part)

        return participants

    @classmethod
    def ingest_afat_fleet(
        cls,
        operation: CIOperation,
        afat_fleet_id: int,
    ) -> list[CIOperationParticipant]:
        """
        Read-only adapter for AFAT / PAP fleet records if the AFAT plugin is installed.
        Supports both FatLink/Fat and AFATFleet/AFATFleetAttendance models.
        """
        try:
            # 1. Try standard AFAT FatLink / Fat models
            try:
                # Third Party
                from afat.models import Fat, FatLink

                fatlink = FatLink.objects.filter(id=afat_fleet_id).first()
                if fatlink:
                    roster = []
                    fats = Fat.objects.filter(fatlink=fatlink).select_related(
                        "character"
                    )
                    for f in fats:
                        char_id = getattr(f.character, "character_id", None) or getattr(
                            f.character, "id", None
                        )
                        char_name = getattr(
                            f.character, "character_name", None
                        ) or getattr(f.character, "name", "")
                        if char_id:
                            roster.append(
                                {
                                    "character_id": char_id,
                                    "character_name": str(char_name),
                                }
                            )
                    fc_user = None
                    if hasattr(fatlink, "character") and fatlink.character:
                        fc_char = fatlink.character
                        fc_user = IdentityResolver.resolve_user_by_character(
                            getattr(fc_char, "character_id", None)
                            or getattr(fc_char, "id", None)
                        )
                    elif hasattr(fatlink, "creator") and fatlink.creator:
                        fc_user = fatlink.creator

                    recorded_time = (
                        getattr(fatlink, "created", None)
                        or getattr(fatlink, "created_at", None)
                        or getattr(fatlink, "start_time", None)
                        or timezone.now()
                    )
                    return cls.ingest_fc_roster(
                        operation=operation,
                        roster_entries=roster,
                        fc_user=fc_user,
                        recorded_at=recorded_time,
                    )
            except (ImportError, AttributeError):
                pass

            # 2. Try AFATFleet / AFATFleetAttendance models
            try:
                # Third Party
                from afat.models import AFATFleet, AFATFleetAttendance

                fleet = AFATFleet.objects.filter(id=afat_fleet_id).first()
                if fleet:
                    roster = []
                    for att in AFATFleetAttendance.objects.filter(
                        fleet=fleet
                    ).select_related("character"):
                        roster.append(
                            {
                                "character_id": att.character.character_id,
                                "character_name": att.character.character_name,
                            }
                        )

                    fc_user = (
                        fleet.fleet_commander.user if fleet.fleet_commander else None
                    )
                    return cls.ingest_fc_roster(
                        operation=operation,
                        roster_entries=roster,
                        fc_user=fc_user,
                        recorded_at=fleet.start_time,
                    )
            except (ImportError, AttributeError):
                pass

            return []
        except Exception:
            # AFAT not installed or error querying models
            return []

    @classmethod
    def sync_recent_afat_fleets(cls, hours: int = 48) -> list[CIOperation]:
        """
        Scans for recent AFAT/ESI fleets and automatically creates or updates
        corresponding CIOperation and participant records in Counterintelligence.
        """
        synced_ops = []
        cutoff = timezone.now() - timezone.timedelta(hours=hours)

        # 1. Sync from FatLink
        try:
            # Third Party
            from afat.models import FatLink

            recent_fatlinks = []
            if hasattr(FatLink, "created"):
                recent_fatlinks = FatLink.objects.filter(created__gte=cutoff)
            elif hasattr(FatLink, "created_at"):
                recent_fatlinks = FatLink.objects.filter(created_at__gte=cutoff)
            elif hasattr(FatLink, "start_time"):
                recent_fatlinks = FatLink.objects.filter(start_time__gte=cutoff)
            else:
                recent_fatlinks = FatLink.objects.all()

            for fl in recent_fatlinks:
                fleet_name = getattr(fl, "fleet", None) or f"AFAT Fleet #{fl.id}"
                start_time = (
                    getattr(fl, "created", None)
                    or getattr(fl, "created_at", None)
                    or getattr(fl, "start_time", None)
                    or timezone.now()
                )
                # Check datetime closed attributes first
                end_time = getattr(fl, "closed_at", None) or getattr(fl, "end_time", None)
                if not end_time:
                    raw_closed = getattr(fl, "closed", None)
                    if hasattr(raw_closed, "strftime"):
                        end_time = raw_closed
                    elif raw_closed is True:
                        end_time = (
                            getattr(fl, "updated_at", None)
                            or getattr(fl, "updated", None)
                            or (start_time + timedelta(hours=2))
                        )
                    elif hasattr(fl, "valid_until") and fl.valid_until and fl.valid_until <= timezone.now():
                        end_time = fl.valid_until

                if end_time and timezone.is_naive(end_time):
                    end_time = timezone.make_aware(end_time)

                fc_user = None
                if hasattr(fl, "character") and fl.character:
                    fc_char = fl.character
                    fc_user = IdentityResolver.resolve_user_by_character(
                        getattr(fc_char, "character_id", None)
                        or getattr(fc_char, "id", None)
                    )
                elif hasattr(fl, "creator") and fl.creator:
                    fc_user = fl.creator

                op, _ = CIOperation.objects.get_or_create(
                    name=str(fleet_name),
                    start_time=start_time,
                    defaults={
                        "end_time": end_time,
                        "operation_type": "strategic",
                        "fleet_commander": fc_user,
                        "notes": f"Synced automatically from AFAT FatLink #{fl.id}",
                    },
                )
                if end_time and op.end_time != end_time:
                    op.end_time = end_time
                    op.save(update_fields=["end_time"])

                cls.ingest_afat_fleet(op, fl.id)
                synced_ops.append(op)
        except Exception:
            pass

        # 2. Sync from AFATFleet
        try:
            # Third Party
            from afat.models import AFATFleet

            recent_fleets = AFATFleet.objects.filter(start_time__gte=cutoff)
            for f in recent_fleets:
                op, _ = CIOperation.objects.get_or_create(
                    name=f.fleet_name or f"AFAT Fleet #{f.id}",
                    start_time=f.start_time,
                    defaults={
                        "end_time": getattr(f, "end_time", None),
                        "operation_type": "strategic",
                        "fleet_commander": (
                            f.fleet_commander.user if f.fleet_commander else None
                        ),
                        "notes": f"Synced automatically from AFAT Fleet #{f.id}",
                    },
                )
                cls.ingest_afat_fleet(op, f.id)
                synced_ops.append(op)
        except Exception:
            pass

        return synced_ops
