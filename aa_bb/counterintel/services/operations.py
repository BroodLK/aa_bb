"""
Operational intelligence service for managing operations, objectives, and timeline reconstruction.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.db.models import Count, Q
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

    DEFAULT_PRE_WINDOW_MINUTES = 15
    DEFAULT_POST_WINDOW_MINUTES = 15

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
    def find_or_create_operation(
        cls,
        name: str,
        start_time,
        end_time=None,
        operation_type: str = "strategic",
        fleet_commander: User = None,
        secrecy_level: str = "standard",
        notes: str = "",
        external_ref: str = "",
        mark_closed: bool = False,
    ) -> tuple[CIOperation, bool]:
        """
        Finds an existing matching operation or creates a new one, preventing duplicate
        operations created by differing sync sources, signal handlers, or microsecond clock jitter.

        external_ref: exact source fleet reference (e.g. "afat_fatlink:123").
        mark_closed: the source reports the fleet as concluded without a timestamp;
        an open operation is closed at the current time, an existing end_time is kept.
        """
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time)
        if end_time and timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time)

        cleaned_name = name.strip()
        op = None

        # 1. Match by exact source fleet reference
        if external_ref:
            op = CIOperation.objects.filter(external_ref=external_ref).first()

        # 2. Adopt an untagged (manually created) operation with the same name
        # within a 2-hour window; operations of other source fleets never match
        if not op:
            window_start = start_time - timedelta(hours=2)
            window_end = start_time + timedelta(hours=2)
            op = (
                CIOperation.objects.filter(
                    name__iexact=cleaned_name,
                    start_time__gte=window_start,
                    start_time__lte=window_end,
                    external_ref="",
                )
                .order_by("id")
                .first()
            )

        # 3. If found, update attributes if new information is provided
        if op:
            updated_fields = []
            if op.window_locked:
                # Manually set by a CI manager; keep their window
                pass
            elif end_time and op.end_time != end_time:
                op.end_time = end_time
                # The window changed, so zKillboard must be scanned again
                op.combat_scanned_at = None
                updated_fields.extend(["end_time", "combat_scanned_at"])
            elif mark_closed and not end_time and op.end_time is None:
                op.end_time = timezone.now()
                op.combat_scanned_at = None
                updated_fields.extend(["end_time", "combat_scanned_at"])
            if fleet_commander and not op.fleet_commander:
                op.fleet_commander = fleet_commander
                updated_fields.append("fleet_commander")
            if external_ref and not op.external_ref:
                op.external_ref = external_ref
                updated_fields.append("external_ref")
                if notes and notes not in op.notes:
                    op.notes = f"{op.notes}\n{notes}".strip()
                    updated_fields.append("notes")
            if updated_fields:
                op.save(update_fields=updated_fields)
            return op, False

        # 4. Otherwise create a new operation
        if mark_closed and not end_time:
            end_time = timezone.now()
        op = CIOperation.objects.create(
            name=cleaned_name,
            start_time=start_time,
            end_time=end_time,
            operation_type=operation_type,
            fleet_commander=fleet_commander,
            secrecy_level=secrecy_level,
            notes=notes,
            external_ref=external_ref,
        )
        return op, True

    @classmethod
    def deduplicate_operations(cls, since_hours: int | None = None) -> int:
        """
        Scans for duplicate operations (matching name within a 2-hour window) and merges
        participants, objectives, and correlations into a single authoritative record,
        pruning orphan duplicate operations. Operations synced from different source
        fleets are never merged. since_hours limits the scan to recent operations.
        Returns the count of merged/deleted duplicate operations.
        """
        from .correlation import CorrelationEngine

        ops_qs = CIOperation.objects.annotate(participant_count=Count("participants"))
        if since_hours is not None:
            ops_qs = ops_qs.filter(
                start_time__gte=timezone.now() - timedelta(hours=since_hours + 2)
            )
        all_ops = list(ops_qs.order_by("start_time", "id"))
        merged_count = 0
        seen_op_ids = set()

        for op in all_ops:
            if op.id in seen_op_ids:
                continue

            window_start = op.start_time - timedelta(hours=2)
            window_end = op.start_time + timedelta(hours=2)

            cluster = [op]
            cluster_ref = op.external_ref
            for dup in all_ops:
                if (
                    dup.id == op.id
                    or dup.id in seen_op_ids
                    or dup.name.strip().lower() != op.name.strip().lower()
                    or not window_start <= dup.start_time <= window_end
                ):
                    continue
                if dup.external_ref and cluster_ref and dup.external_ref != cluster_ref:
                    continue
                cluster.append(dup)
                cluster_ref = cluster_ref or dup.external_ref

            if len(cluster) == 1:
                continue

            # Pick the best primary record: prefer the source-synced one, then a
            # manager-set window, then one with end_time, most participants, lowest id
            primary = max(
                cluster,
                key=lambda o: (
                    1 if o.external_ref else 0,
                    1 if o.window_locked else 0,
                    1 if o.end_time else 0,
                    o.participant_count,
                    -o.id,
                ),
            )

            for other in cluster:
                if other.id == primary.id:
                    continue

                seen_op_ids.add(other.id)

                # Merge end_time, keeping a manager-set window intact
                if (
                    not primary.window_locked
                    and not primary.end_time
                    and other.end_time
                ):
                    primary.end_time = other.end_time

                # Merge FC
                if not primary.fleet_commander and other.fleet_commander:
                    primary.fleet_commander = other.fleet_commander

                # Merge participants
                for p in other.participants.all():
                    if not primary.participants.filter(
                        character_id=p.character_id
                    ).exists():
                        p.operation = primary
                        p.save(update_fields=["operation"])

                # Merge objectives
                for obj in other.objectives.all():
                    obj.operation = primary
                    obj.save(update_fields=["operation"])

                # Merge correlations
                for corr in other.correlations.all():
                    corr.operation = primary
                    corr.save(update_fields=["operation"])

                # Merge notes
                if other.notes and other.notes not in primary.notes:
                    primary.notes = f"{primary.notes}\n{other.notes}".strip()

                other.delete()
                merged_count += 1

            primary.save()
            CorrelationEngine.correlate_operation(primary)
            seen_op_ids.add(primary.id)

        return merged_count

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
        window_start = operation.start_time - timedelta(minutes=pre_minutes)
        window_end = operation.effective_end() + timedelta(minutes=post_minutes)
        return window_start, window_end

    @classmethod
    def exposure_start(cls, operation: CIOperation):
        """
        When members could first have learned of the operation: the earliest
        objective disclosure, or the pre-window before its start.
        """
        disclosures = [
            obj.disclosure_time
            for obj in operation.objectives.all()
            if obj.disclosure_time
        ]
        default = operation.start_time - timedelta(
            minutes=cls.DEFAULT_PRE_WINDOW_MINUTES
        )
        return min([default, *disclosures])

    @classmethod
    def exposed_user_ids(cls, operation: CIOperation) -> set:
        """
        Members who knew of the operation before it started: the FC roster plus
        anyone in comms between its disclosure and its start.
        """
        roster = set(
            operation.participants.exclude(user__isnull=True).values_list(
                "user_id", flat=True
            )
        )
        comms = set(
            CISession.objects.filter(
                start_time__lte=operation.start_time,
            )
            .filter(
                Q(end_time__isnull=True)
                | Q(end_time__gte=cls.exposure_start(operation))
            )
            .values_list("user_id", flat=True)
        )
        return roster | comms

    @classmethod
    def _audited_character_ids(cls, char_ids: list) -> set | None:
        """
        Character IDs with a corptools CharacterAudit, or None when corptools
        is unavailable and audit status cannot be determined.
        """
        # AA BigBrother
        from aa_bb.app_settings import corptools_active

        if not corptools_active():
            return None
        try:
            # Third Party
            from corptools.models import CharacterAudit
        except ImportError:
            return None
        return set(
            CharacterAudit.objects.filter(
                character__character_id__in=char_ids
            ).values_list("character__character_id", flat=True)
        )

    @classmethod
    def _summarize_comms(cls, sessions: list, window_start, window_end) -> list:
        """
        One row per user: time spent in comms within the window, plus first login
        and last logoff. Login/logoff outside the window are reported as None.
        """
        now = timezone.now()
        rows = {}
        for s in sessions:
            row = rows.setdefault(
                s["user_id"],
                {
                    "user_id": s["user_id"],
                    "username": s["username"],
                    "channels": set(),
                    "total_seconds": 0.0,
                    "first_start": s["start_time"],
                    "last_end": s["end_time"],
                    "still_connected": False,
                },
            )
            if s["channel_name"]:
                row["channels"].add(s["channel_name"])
            overlap_end = min(window_end, s["end_time"] or now)
            overlap_start = max(window_start, s["start_time"])
            row["total_seconds"] += max(
                0.0, (overlap_end - overlap_start).total_seconds()
            )
            row["first_start"] = min(row["first_start"], s["start_time"])
            if s["end_time"] is None:
                row["still_connected"] = True
            elif row["last_end"] is None or s["end_time"] > row["last_end"]:
                row["last_end"] = s["end_time"]

        summary = []
        for row in rows.values():
            last_end = None if row["still_connected"] else row["last_end"]
            total = int(row["total_seconds"])
            summary.append(
                {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "channels": sorted(row["channels"]),
                    "total_seconds": total,
                    "total_display": f"{total // 3600}:{(total % 3600) // 60:02d}",
                    "login": (
                        row["first_start"]
                        if row["first_start"] >= window_start
                        else None
                    ),
                    "logoff": (
                        last_end if last_end and last_end <= window_end else None
                    ),
                }
            )
        summary.sort(
            key=lambda r: (r["login"] is None, r["login"] or now, r["username"])
        )
        return summary

    @classmethod
    def _summarize_combat(cls, events: list, fleet_char_ids: set) -> list:
        """
        Killmails in time order, labelled as a Loss when the victim was in the
        fleet and a Kill otherwise.
        """
        killmails = [ev for ev in events if ev["source"] == "zkill"]
        combat = []
        for ev in sorted(killmails, key=lambda e: e["timestamp"]):
            payload = ev["payload"] or {}
            combat.append(
                {
                    "kind": (
                        "Loss"
                        if payload.get("victim_character_id") in fleet_char_ids
                        else "Kill"
                    ),
                    "timestamp": ev["timestamp"],
                    "killmail_id": payload.get("killmail_id"),
                }
            )
        return combat

    @classmethod
    def redact_for_display(cls, timeline: dict) -> dict:
        """
        Copy of a reconstructed timeline for display. When the privacy setting
        is on, Discord channel/thread names are blanked; detection always runs
        on the unredacted timeline, so scores are unaffected.
        """
        # AA BigBrother
        from aa_bb.counterintel.scoring import scoring

        if not scoring().hide_discord_channel_names:
            return timeline

        def strip_payload(payload: dict) -> dict:
            payload = dict(payload or {})
            for key in (
                "channel_name",
                "channel",
                "thread_name",
                "parent_channel_name",
            ):
                payload.pop(key, None)
            payload["channel_hidden"] = True
            return payload

        events = [
            (
                {**ev, "payload": strip_payload(ev["payload"])}
                if ev["source"] == "discord"
                else ev
            )
            for ev in timeline["events"]
        ]
        sessions = [
            {**s, "channel_name": ""} if s["source"] == "discord" else s
            for s in timeline["sessions"]
        ]
        detected_channels = {s["channel_name"] for s in sessions if s["channel_name"]}
        for ev in events:
            payload = ev.get("payload") or {}
            channel = payload.get("channel_name") or payload.get("channel")
            if channel:
                detected_channels.add(channel)

        return {
            **timeline,
            "events": events,
            "sessions": sessions,
            "comms_summary": cls._summarize_comms(
                sessions, timeline["window_start"], timeline["window_end"]
            ),
            "detected_channels": sorted(detected_channels),
            "discord_channels_hidden": True,
        }

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

        # Retrieve direct events. Killmails are bound to the window so that
        # editing the operation times drops ones scanned for an earlier window.
        events_qs = (
            CIEvent.objects.filter(
                Q(timestamp__gte=window_start, timestamp__lte=window_end)
                | (Q(operation=operation) & ~Q(source="zkill"))
            )
            .select_related("user__profile__main_character")
            .order_by("timestamp")
        )

        events_list = []
        unique_users_seen = set()
        for ev in events_qs:
            if ev.user:
                unique_users_seen.add(ev.user_id)
            main_character = getattr(
                getattr(getattr(ev.user, "profile", None), "main_character", None),
                "character_name",
                None,
            )
            payload = ev.payload or {}
            tracked_character = payload.get("character_name")
            # Unlinked members are labelled with whatever name the source reported
            external_name = (
                payload.get("discord_display_name")
                or payload.get("discord_username")
                or payload.get("username")
            )
            user_label = main_character or (
                ev.user.username if ev.user else (external_name or "Unknown")
            )
            member_label = user_label
            if tracked_character and tracked_character != main_character:
                member_label = f"{member_label} ({tracked_character})"

            events_list.append(
                {
                    "id": ev.id,
                    "source": ev.source,
                    "source_event_id": ev.source_event_id,
                    "timestamp": ev.timestamp,
                    "user_id": ev.user_id,
                    "username": member_label,
                    "user_label": user_label,
                    "character_name": tracked_character or "",
                    "payload": ev.payload,
                }
            )

        # Retrieve comms sessions overlapping the window
        sessions_qs = (
            CISession.objects.filter(
                start_time__lte=window_end,
            )
            .filter(Q(end_time__isnull=True) | Q(end_time__gte=window_start))
            .select_related("user__profile__main_character")
            .order_by("start_time")
        )

        sessions_list = []
        for s in sessions_qs:
            unique_users_seen.add(s.user_id)
            main_character = getattr(
                getattr(getattr(s.user, "profile", None), "main_character", None),
                "character_name",
                None,
            )
            sessions_list.append(
                {
                    "id": s.id,
                    "source": s.source,
                    "user_id": s.user_id,
                    "username": main_character or s.user.username,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "channel_name": s.channel_name,
                    "reconnect_count": s.reconnect_count,
                    "duration_seconds": s.duration_seconds,
                }
            )

        # Objectives
        objectives = list(operation.objectives.all())
        objectives_list = [
            {
                "id": obj.id,
                "name": obj.name,
                "objective_type": obj.objective_type,
                "expected_roles": obj.expected_roles,
                "disclosure_time": obj.disclosure_time,
            }
            for obj in objectives
        ]

        # FC confirmed participants & context role tagging
        participants_qs = (
            operation.participants.select_related("user")
            .all()
            .order_by("recorded_at", "character_id")
        )
        confirmed_user_ids = {p.user_id for p in participants_qs}
        char_ids = [p.character_id for p in participants_qs]

        # Batch resolve character names from EveCharacter / CIIdentity
        char_names_map = {}
        try:
            # Alliance Auth
            from allianceauth.eveonline.models import EveCharacter

            for ec in EveCharacter.objects.filter(character_id__in=char_ids):
                char_names_map[ec.character_id] = ec.character_name
        except Exception:
            pass

        audited_char_ids = cls._audited_character_ids(char_ids)

        # Objective roles are the same for every participant
        objective_tags = []
        for obj in objectives:
            if obj.expected_roles:
                objective_tags.extend(obj.expected_roles)
            else:
                objective_tags.append(obj.objective_type.replace("_", " ").title())

        user_role_tags = {}
        for p in participants_qs:
            tags = ["FC"] if operation.fleet_commander_id == p.user_id else []
            tags.extend(objective_tags)
            user_role_tags[p.user_id] = list(dict.fromkeys(tags)) or ["Combat Fleet"]

        # Index activity once by character instead of scanning per participant
        login_char_ids = set()
        logoff_char_ids = set()
        killmail_char_ids = set()
        for ev in events_list:
            payload = ev["payload"] or {}
            if ev["source"] == "eve_activity":
                if payload.get("activity_type") == "login":
                    login_char_ids.add(payload.get("character_id"))
                elif payload.get("activity_type") == "logoff":
                    logoff_char_ids.add(payload.get("character_id"))
            elif ev["source"] == "zkill":
                killmail_char_ids.update(payload.get("attacker_character_ids") or [])
                killmail_char_ids.add(payload.get("victim_character_id"))
        comms_user_ids = {s["user_id"] for s in sessions_list}

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
                    "logged_in_near_fleet": p.character_id in login_char_ids,
                    "logged_off_near_fleet": p.character_id in logoff_char_ids,
                    "joined_comms": p.user_id in comms_user_ids,
                    "joined_fleet": True,
                    "on_killmail": p.character_id in killmail_char_ids,
                    # None when corptools is unavailable and audits are unknown
                    "has_audit": (
                        None
                        if audited_char_ids is None
                        else p.character_id in audited_char_ids
                    ),
                }
            )

        # Add comms-only users to the roster summary so officers can see them
        # without confusing them with FC-confirmed fleet participants.
        participant_user_ids = {p["user_id"] for p in participants_list}
        first_session_by_user = {}
        for session in sessions_list:
            if session["user_id"] not in participant_user_ids:
                first_session_by_user.setdefault(session["user_id"], session)
        comms_only_users = User.objects.filter(
            id__in=first_session_by_user
        ).select_related("profile__main_character")
        for user in sorted(
            comms_only_users,
            key=lambda u: first_session_by_user[u.id]["start_time"],
        ):
            session = first_session_by_user[user.id]
            main = getattr(user.profile.main_character, "character_name", None)
            participants_list.append(
                {
                    "id": None,
                    "character_id": None,
                    "character_name": main or user.username,
                    "user_id": user.id,
                    "username": user.username,
                    "confirmed_by_fc": False,
                    "recorded_at": session["start_time"],
                    "role_context": ["Comms only"],
                    "logged_in_near_fleet": False,
                    "logged_off_near_fleet": False,
                    "joined_comms": True,
                    "joined_fleet": False,
                    "on_killmail": False,
                    "has_audit": None,
                }
            )

        # Enrich events with user role context tags
        for ev in events_list:
            if ev["user_id"]:
                ev["role_context"] = user_role_tags.get(ev["user_id"], [])
            else:
                ev["role_context"] = []

        # Participation and comms overlap metrics
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
            "event_users": sorted({ev["user_label"] for ev in events_list}),
            "sessions": sessions_list,
            "comms_summary": cls._summarize_comms(
                sessions_list, window_start, window_end
            ),
            "combat_events": cls._summarize_combat(events_list, set(char_ids)),
            "participants": participants_list,
            "detected_channels": sorted(list(detected_channels)),
            "metrics": {
                "total_events": len(events_list),
                "total_sessions": len(sessions_list),
                "total_confirmed_participants": sum(
                    1 for p in participants_list if p["confirmed_by_fc"]
                ),
                "unique_users_observed": len(unique_users_seen),
                "comms_without_pap_count": len(comms_without_pap_users),
                "pap_without_comms_count": len(pap_without_comms_users),
            },
        }
