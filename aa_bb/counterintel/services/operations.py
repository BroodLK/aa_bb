"""
Operational intelligence service for managing operations, objectives, and timeline reconstruction.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from ..models import (
    CIEvent,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CISession,
)
from .identity import IdentityResolver


class CIOperationService:
    """
    Manages operations, objective tagging, FC-submitted participants,
    and operational timeline reconstruction across all intelligence sources.
    """

    DEFAULT_PRE_WINDOW_MINUTES = 30
    DEFAULT_POST_WINDOW_MINUTES = 30

    @classmethod
    def create_operation(
        cls,
        name: str,
        start_time,
        end_time=None,
        operation_type: str = "strategic",
        fleet_commander: User = None,
        secrecy_level: str = "standard",
        notes: str = "",
    ) -> CIOperation:
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time)
        if end_time and timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time)

        return CIOperation.objects.create(
            name=name,
            start_time=start_time,
            end_time=end_time,
            operation_type=operation_type,
            fleet_commander=fleet_commander,
            secrecy_level=secrecy_level,
            notes=notes,
        )

    @classmethod
    def add_objective(
        cls,
        operation: CIOperation,
        name: str,
        objective_type: str,
        expected_roles: list = None,
        disclosure_time=None,
    ) -> CIOperationObjective:
        if disclosure_time and timezone.is_naive(disclosure_time):
            disclosure_time = timezone.make_aware(disclosure_time)

        return CIOperationObjective.objects.create(
            operation=operation,
            name=name,
            objective_type=objective_type,
            expected_roles=expected_roles or [],
            disclosure_time=disclosure_time,
        )

    @classmethod
    def add_participant(
        cls,
        operation: CIOperation,
        character_id: int,
        user: User = None,
        confirmed_by_fc: bool = True,
        recorded_at=None,
    ) -> CIOperationParticipant | None:
        rec_at = recorded_at or timezone.now()
        if timezone.is_naive(rec_at):
            rec_at = timezone.make_aware(rec_at)

        resolved_user = user or IdentityResolver.resolve_user_by_character(
            character_id, at_time=rec_at
        )
        if not resolved_user:
            # Fallback or create provisional identity if needed
            return None

        participant, _ = CIOperationParticipant.objects.update_or_create(
            operation=operation,
            character_id=character_id,
            defaults={
                "user": resolved_user,
                "confirmed_by_fc": confirmed_by_fc,
                "recorded_at": rec_at,
            },
        )
        return participant

    @classmethod
    def get_operational_window(
        cls,
        operation: CIOperation,
        pre_minutes: int = DEFAULT_PRE_WINDOW_MINUTES,
        post_minutes: int = DEFAULT_POST_WINDOW_MINUTES,
    ) -> tuple:
        op_start = operation.start_time
        op_end = operation.end_time or (op_start + timedelta(hours=2))
        window_start = op_start - timedelta(minutes=pre_minutes)
        window_end = op_end + timedelta(minutes=post_minutes)
        return window_start, window_end

    @classmethod
    def reconstruct_timeline(
        cls,
        operation: CIOperation,
        pre_minutes: int = DEFAULT_PRE_WINDOW_MINUTES,
        post_minutes: int = DEFAULT_POST_WINDOW_MINUTES,
    ) -> dict:
        """
        Reconstruct complete operational timeline across CIEvents, CISessions,
        FC participants, and combat data within the operational window.
        """
        window_start, window_end = cls.get_operational_window(
            operation, pre_minutes, post_minutes
        )

        # Retrieve direct events
        events_qs = (
            CIEvent.objects.filter(
                Q(operation=operation)
                | Q(timestamp__gte=window_start, timestamp__lte=window_end)
            )
            .select_related("user")
            .order_by("timestamp")
        )

        events_list = []
        unique_users_seen = set()
        for ev in events_qs:
            if ev.user:
                unique_users_seen.add(ev.user_id)
            events_list.append(
                {
                    "id": ev.id,
                    "source": ev.source,
                    "source_event_id": ev.source_event_id,
                    "timestamp": ev.timestamp,
                    "user_id": ev.user_id,
                    "username": ev.user.username if ev.user else "Unknown",
                    "payload": ev.payload,
                }
            )

        # Retrieve comms sessions overlapping the window
        sessions_qs = (
            CISession.objects.filter(
                start_time__lte=window_end,
            )
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=window_start))
            .select_related("user")
            .order_by("start_time")
        )

        sessions_list = []
        for s in sessions_qs:
            unique_users_seen.add(s.user_id)
            sessions_list.append(
                {
                    "id": s.id,
                    "source": s.source,
                    "user_id": s.user_id,
                    "username": s.user.username,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "channel_name": s.channel_name,
                    "reconnect_count": s.reconnect_count,
                    "duration_seconds": s.duration_seconds,
                }
            )

        # Objectives
        objectives_list = [
            {
                "id": obj.id,
                "name": obj.name,
                "objective_type": obj.objective_type,
                "expected_roles": obj.expected_roles,
                "disclosure_time": obj.disclosure_time,
            }
            for obj in operation.objectives.all()
        ]

        # FC confirmed participants & context role tagging
        participants_qs = operation.participants.select_related("user").all().order_by("recorded_at", "character_id")
        confirmed_user_ids = {p.user_id for p in participants_qs}
        char_ids = [p.character_id for p in participants_qs]

        # Batch resolve character names from EveCharacter / CIIdentity
        char_names_map = {}
        try:
            from allianceauth.eveonline.models import EveCharacter

            for ec in EveCharacter.objects.filter(character_id__in=char_ids):
                char_names_map[ec.character_id] = ec.character_name
        except Exception:
            pass

        user_role_tags = {}
        for p in participants_qs:
            tags = []
            if operation.fleet_commander_id == p.user_id:
                tags.append("FC")
            for obj in operation.objectives.all():
                if obj.expected_roles:
                    tags.extend(obj.expected_roles)
                else:
                    tags.append(obj.objective_type.replace("_", " ").title())
            if not tags:
                tags.append("Combat Fleet")
            user_role_tags[p.user_id] = list(dict.fromkeys(tags))

        participants_list = []
        seen_char_ids = set()
        for p in participants_qs:
            if p.character_id in seen_char_ids:
                continue
            seen_char_ids.add(p.character_id)
            cname = char_names_map.get(p.character_id) or p.user.username
            participants_list.append(
                {
                    "id": p.id,
                    "character_id": p.character_id,
                    "character_name": cname,
                    "user_id": p.user_id,
                    "username": p.user.username,
                    "confirmed_by_fc": p.confirmed_by_fc,
                    "recorded_at": p.recorded_at,
                    "role_context": user_role_tags.get(p.user_id, ["Combat Fleet"]),
                }
            )

        # Enrich events with user role context tags
        for ev in events_list:
            if ev["user_id"]:
                ev["role_context"] = user_role_tags.get(ev["user_id"], [])
            else:
                ev["role_context"] = []

        # Participation and comms overlap metrics
        comms_user_ids = {s["user_id"] for s in sessions_list}
        comms_without_pap_users = comms_user_ids - confirmed_user_ids
        pap_without_comms_users = confirmed_user_ids - comms_user_ids

        # Extract all detected channel names from sessions and voice events
        detected_channels = set()
        for s in sessions_list:
            if s.get("channel_name"):
                detected_channels.add(s["channel_name"])
        for ev in events_list:
            payload = ev.get("payload") or {}
            ch = payload.get("channel_name") or payload.get("channel")
            if ch:
                detected_channels.add(ch)

        return {
            "operation": {
                "id": operation.id,
                "name": operation.name,
                "operation_type": operation.operation_type,
                "secrecy_level": operation.secrecy_level,
                "start_time": operation.start_time,
                "end_time": operation.end_time,
                "is_active": operation.is_active,
                "is_concluded": operation.is_concluded,
                "status_label": operation.status_label,
                "fleet_commander": (
                    operation.fleet_commander.username
                    if operation.fleet_commander
                    else None
                ),
                "fleet_commander_id": operation.fleet_commander_id,
                "notes": operation.notes,
            },
            "window_start": window_start,
            "window_end": window_end,
            "objectives": objectives_list,
            "events": events_list,
            "sessions": sessions_list,
            "participants": participants_list,
            "detected_channels": sorted(list(detected_channels)),
            "metrics": {
                "total_events": len(events_list),
                "total_sessions": len(sessions_list),
                "total_confirmed_participants": len(participants_list),
                "unique_users_observed": len(unique_users_seen),
                "comms_without_pap_count": len(comms_without_pap_users),
                "pap_without_comms_count": len(pap_without_comms_users),
            },
        }
