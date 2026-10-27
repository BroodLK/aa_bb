"""
Idempotent Mumble ingestion and session tracking.

Two sources feed Mumble presence. The Alliance Auth authenticator log
(``MUMBLE_AUTHENTICATOR_LOG_PATH``) records every successful and failed
authentication with second precision. Alliance Auth's ``MumbleUser.last_connect``
/ ``last_disconnect`` columns are updated by the authenticator from Murmur's ICE
callbacks and are the only source of disconnects, since the authenticator never
logs them.

Sessions are derived from the stored events by replaying them chronologically,
so events arriving out of order from the two sources still produce correct
session intervals.
"""

# Standard Library
import logging
import os
import re
from datetime import datetime, timedelta

# Django
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from ..models import CIEvent, CILogIngestState, CISession
from ..services.identity import IdentityResolver

logger = logging.getLogger(__name__)

DEFAULT_MUMBLE_ID_OFFSET = 1000000000


class MumbleLogIngestor:
    """
    Parses Alliance Auth Mumble authenticator logs into normalized CIEvents
    and maintains contiguous CISession records.
    """

    RECONNECT_THRESHOLD_SECONDS = 300  # 5 minutes
    # A log connect and a presence-poll connect for the same login are the same event
    CONNECT_MERGE_SECONDS = 60
    SESSION_ACTIONS = ("connect", "disconnect", "channel_move")

    TIMESTAMP_FORMAT = "%d/%b/%Y %H:%M:%S"

    # [26/Oct/2026 07:40:09] INFO [mumble_authenticator:599] <message>
    _PREFIX = (
        r"^\[(?P<timestamp>\d{1,2}/[A-Za-z]{3}/\d{4}\s+\d{2}:\d{2}:\d{2})\]\s+"
        r"\w+\s+\[[^\]]+\]\s+"
    )

    # (action, id_kind, pattern). id_kind tells how to read the trailing number:
    #   "uid"      -> AA user pk + MumbleServerServer.offset
    #   "temp_uid" -> TempUser pk + 2 * offset (no AA user)
    #   "user_pk"  -> AA user pk without offset (failed auth lines)
    PATTERNS = [
        (
            "connect",
            "temp_uid",
            re.compile(
                _PREFIX
                + r"TEMP User authenticated:\s*(?P<name>.+?)\s+(?P<number>\d+)\s*$"
            ),
        ),
        (
            "connect",
            "uid",
            re.compile(
                _PREFIX + r"User authenticated:\s*(?P<name>.+?)\s+(?P<number>\d+)\s*$"
            ),
        ),
        (
            "auth_failed",
            "temp_uid",
            re.compile(
                _PREFIX + r"Failed authentication attempt for TEMP user:\s*"
                r"(?P<name>.+?)\s+(?P<number>\d+)\s*$"
            ),
        ),
        (
            "auth_failed",
            "user_pk",
            re.compile(
                _PREFIX + r"Failed authentication attempt for user:\s*"
                r"(?P<name>.+?)\s+(?P<number>\d+)\s*$"
            ),
        ),
    ]

    @classmethod
    def get_id_offset(cls) -> int:
        """Mumble user id offset configured on the AA Mumble server."""
        try:
            # Alliance Auth
            from allianceauth.services.modules.mumble.models import (
                MumbleServerServer,
            )

            server = MumbleServerServer.objects.order_by("pk").first()
            if server and server.offset:
                return int(server.offset)
        except Exception:
            pass
        return DEFAULT_MUMBLE_ID_OFFSET

    @classmethod
    def parse_log_line(cls, line: str) -> dict | None:
        """
        Parse a single authenticator log line. Lines that carry no presence
        information (group memberships, debug output, ICE housekeeping) return None.
        """
        line = line.strip()
        if not line:
            return None

        for action, id_kind, pattern in cls.PATTERNS:
            m = pattern.match(line)
            if not m:
                continue

            try:
                parsed_dt = datetime.strptime(
                    m.group("timestamp"), cls.TIMESTAMP_FORMAT
                )
            except ValueError:
                return None
            if timezone.is_naive(parsed_dt):
                parsed_dt = timezone.make_aware(parsed_dt)

            return {
                "timestamp": parsed_dt,
                "username": m.group("name").strip(),
                "action": action,
                "id_kind": id_kind,
                "number": int(m.group("number")),
            }

        return None

    @classmethod
    def resolve_parsed_user(cls, parsed: dict, offset: int = None) -> User | None:
        id_kind = parsed.get("id_kind")
        number = parsed.get("number")
        username = parsed.get("username", "")

        if id_kind == "temp_uid":
            return None

        user = None
        if number is not None:
            if id_kind == "uid":
                offset = cls.get_id_offset() if offset is None else offset
                user_pk = number - offset
            else:
                user_pk = number
            if user_pk > 0:
                user = User.objects.filter(pk=user_pk).first()

        if not user and username:
            user = IdentityResolver.resolve_user_by_mumble(
                username, at_time=parsed["timestamp"]
            )
        return user

    @classmethod
    def ingest_log_line(
        cls, line: str, offset: int = None, rebuild_sessions: bool = True
    ) -> CIEvent | None:
        parsed = cls.parse_log_line(line)
        if not parsed:
            return None

        mumble_user_id = (
            str(parsed["number"]) if parsed["id_kind"] in ("uid", "temp_uid") else ""
        )
        return cls.ingest_raw_event(
            username=parsed["username"],
            action=parsed["action"],
            timestamp=parsed["timestamp"],
            user=cls.resolve_parsed_user(parsed, offset=offset),
            mumble_user_id=mumble_user_id,
            temp_user=parsed["id_kind"] == "temp_uid",
            metadata={"raw_line": line.strip(), "source": "authenticator_log"},
            rebuild_sessions=rebuild_sessions,
        )

    @classmethod
    def ingest_log_text(cls, text: str) -> list[CIEvent]:
        return cls._ingest_lines(text.splitlines())

    @classmethod
    def _ingest_lines(cls, lines) -> list[CIEvent]:
        offset = cls.get_id_offset()
        events = []
        rebuild_from = {}
        for line in lines:
            ev = cls.ingest_log_line(line, offset=offset, rebuild_sessions=False)
            if not ev:
                continue
            events.append(ev)
            cls._track_rebuild(rebuild_from, ev)
        cls.rebuild_sessions_for(rebuild_from)
        return events

    @classmethod
    def ingest_log_file(cls, file_path: str, use_offsets: bool = True) -> list[CIEvent]:
        """
        Reads and ingests a Mumble authenticator log from disk idempotently,
        using incremental seek offsets to minimize disk I/O and RAM usage.
        """
        if not os.path.exists(file_path):
            return []

        state = None
        start_offset = 0
        current_inode = None

        if use_offsets:
            try:
                stat_res = os.stat(file_path)
                current_inode = getattr(stat_res, "st_ino", None)
                file_size = stat_res.st_size

                state, _ = CILogIngestState.objects.get_or_create(
                    file_path=file_path,
                    defaults={
                        "last_inode": current_inode,
                        "last_offset": 0,
                    },
                )

                # Reset offset if file truncated or rotated
                if file_size < state.last_offset or (
                    state.last_inode
                    and current_inode
                    and state.last_inode != current_inode
                ):
                    start_offset = 0
                else:
                    start_offset = state.last_offset
            except Exception:
                logger.exception("Could not load ingest offset for %s", file_path)
                start_offset = 0

        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                if start_offset > 0:
                    f.seek(start_offset)

                events = cls._ingest_lines(f)

                if state:
                    state.last_offset = f.tell()
                    state.last_inode = current_inode
                    state.last_ingested_at = timezone.now()
                    state.save(
                        update_fields=["last_offset", "last_inode", "last_ingested_at"]
                    )
        except Exception:
            logger.exception("Failed to ingest Mumble log %s", file_path)
            return []
        return events

    @classmethod
    def ingest_raw_event(
        cls,
        username: str,
        action: str,
        timestamp,
        channel_name: str = "",
        user: User = None,
        mumble_user_id: str = "",
        temp_user: bool = False,
        metadata: dict = None,
        source_event_id: str = None,
        rebuild_sessions: bool = True,
    ) -> CIEvent:
        """
        Store a normalized Mumble event. ``event.ci_created`` reports whether a new
        row was written. Sessions are rebuilt immediately unless the caller batches
        the rebuild itself (``rebuild_sessions=False``).
        """
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)

        if user is None and username and not temp_user:
            user = IdentityResolver.resolve_user_by_mumble(username, at_time=timestamp)

        # The authenticator log and the presence poll both observe each login.
        if action == "connect" and user:
            window = timedelta(seconds=cls.CONNECT_MERGE_SECONDS)
            existing = (
                CIEvent.objects.filter(
                    source="mumble",
                    user=user,
                    payload__action="connect",
                    timestamp__gte=timestamp - window,
                    timestamp__lte=timestamp + window,
                )
                .order_by("timestamp")
                .first()
            )
            if existing:
                existing.ci_created = False
                return existing

        identity_key = user.id if user else (username or "unknown")
        source_event_id = (
            source_event_id
            or f"mumble:{action}:{identity_key}:{int(timestamp.timestamp())}"
        )
        payload = {
            "username": username,
            "action": action,
            "channel_name": channel_name,
            "mumble_user_id": str(mumble_user_id or ""),
            "temp_user": temp_user,
            "metadata": metadata or {},
        }
        dedup_hash = CIEvent.generate_dedup_hash(
            "mumble", source_event_id, timestamp, payload
        )

        event, created = CIEvent.objects.get_or_create(
            dedup_hash=dedup_hash,
            defaults={
                "source": "mumble",
                "source_event_id": source_event_id,
                "timestamp": timestamp,
                "user": user,
                "payload": payload,
            },
        )
        event.ci_created = created

        if created and user and rebuild_sessions and action in cls.SESSION_ACTIONS:
            cls.rebuild_user_sessions(user, since=timestamp)

        return event

    @classmethod
    def _track_rebuild(cls, rebuild_from: dict, event: CIEvent):
        if not getattr(event, "ci_created", False) or not event.user_id:
            return
        if (event.payload or {}).get("action") not in cls.SESSION_ACTIONS:
            return
        current = rebuild_from.get(event.user_id)
        if current is None or event.timestamp < current:
            rebuild_from[event.user_id] = event.timestamp

    @classmethod
    def rebuild_sessions_for(cls, rebuild_from: dict):
        for user_id, since in rebuild_from.items():
            user = User.objects.filter(id=user_id).first()
            if user:
                cls.rebuild_user_sessions(user, since=since)

    @classmethod
    def rebuild_user_sessions(cls, user: User, since=None):
        """
        Re-derive a user's Mumble sessions from stored events at or after ``since``
        by replaying connect / disconnect / channel_move events in order.
        """
        threshold = timedelta(seconds=cls.RECONNECT_THRESHOLD_SECONDS)
        sessions = CISession.objects.filter(user=user, source="mumble")

        anchor = since
        if anchor is not None:
            anchor = anchor - threshold
            covering_starts = list(
                sessions.filter(start_time__lte=anchor)
                .filter(Q(end_time__isnull=True) | Q(end_time__gte=anchor))
                .values_list("start_time", flat=True)
            )
            if covering_starts:
                anchor = min(covering_starts)
            sessions.filter(start_time__gte=anchor).delete()
            previous = (
                sessions.filter(start_time__lt=anchor).order_by("-start_time").first()
            )
        else:
            sessions.delete()
            previous = None

        events = CIEvent.objects.filter(
            source="mumble",
            user=user,
            payload__action__in=cls.SESSION_ACTIONS,
        )
        if anchor is not None:
            events = events.filter(timestamp__gte=anchor)

        current = previous if previous and previous.end_time is None else None
        last_closed = previous if previous and previous.end_time else None

        for ev in events.order_by("timestamp", "id"):
            action = ev.payload.get("action")
            channel_name = ev.payload.get("channel_name") or ""
            ts = ev.timestamp

            if action == "connect":
                if current:
                    if channel_name:
                        current.channel_name = channel_name
                        current.save(update_fields=["channel_name"])
                elif last_closed and ts - last_closed.end_time <= threshold:
                    # Reconnection after brief drop
                    last_closed.end_time = None
                    last_closed.reconnect_count += 1
                    if channel_name:
                        last_closed.channel_name = channel_name
                    last_closed.save(
                        update_fields=["end_time", "reconnect_count", "channel_name"]
                    )
                    current, last_closed = last_closed, None
                else:
                    current = CISession.objects.create(
                        user=user,
                        source="mumble",
                        start_time=ts,
                        channel_name=channel_name,
                        reconnect_count=0,
                    )

            elif action == "disconnect":
                if current:
                    current.end_time = ts
                    current.save(update_fields=["end_time"])
                    current, last_closed = None, current

            elif action == "channel_move":
                if current:
                    current.channel_name = channel_name
                    current.save(update_fields=["channel_name"])
                else:
                    current = CISession.objects.create(
                        user=user,
                        source="mumble",
                        start_time=ts,
                        channel_name=channel_name,
                        reconnect_count=0,
                    )


class MumblePresenceIngestor:
    """
    Polls Alliance Auth's ``MumbleUser.last_connect`` / ``last_disconnect`` columns,
    which the AA authenticator maintains from Murmur ICE callbacks, and turns them
    into connect / disconnect CIEvents.
    """

    @classmethod
    def get_mumble_users(cls):
        try:
            # Alliance Auth
            from allianceauth.services.modules.mumble.models import MumbleUser

            return (
                MumbleUser.objects.select_related("user")
                .exclude(last_connect__isnull=True, last_disconnect__isnull=True)
                .iterator()
            )
        except Exception:
            logger.debug("Alliance Auth Mumble service not installed; skipping poll.")
            return []

    @classmethod
    def poll(cls, mumble_users=None) -> list[CIEvent]:
        """
        Ingest connect/disconnect observations. ``mumble_users`` defaults to all
        MumbleUser rows with a recorded connection; returns newly created events.
        """
        if mumble_users is None:
            mumble_users = cls.get_mumble_users()

        events = []
        rebuild_from = {}
        for mu in mumble_users:
            observations = [
                (ts, action)
                for ts, action in (
                    (mu.last_connect, "connect"),
                    (mu.last_disconnect, "disconnect"),
                )
                if ts
            ]
            for ts, action in sorted(observations):
                ev = MumbleLogIngestor.ingest_raw_event(
                    username=mu.username,
                    action=action,
                    timestamp=ts,
                    user=mu.user,
                    source_event_id=(
                        f"mumble:presence:{mu.user_id}:{action}:{ts.timestamp()}"
                    ),
                    metadata={"source": "mumble_user_presence"},
                    rebuild_sessions=False,
                )
                events.append(ev)
                MumbleLogIngestor._track_rebuild(rebuild_from, ev)

        MumbleLogIngestor.rebuild_sessions_for(rebuild_from)
        return [ev for ev in events if getattr(ev, "ci_created", False)]
