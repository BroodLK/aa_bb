"""
Idempotent Mumble log ingestor and session tracker.
"""

# Standard Library
import re
from datetime import datetime, timedelta

# Django
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import CIEvent, CILogIngestState, CISession
from ..services.identity import IdentityResolver


class MumbleLogIngestor:
    """
    Parses Mumble authenticator and server logs into normalized CIEvents
    and manages contiguous CISession records.
    """

    RECONNECT_THRESHOLD_SECONDS = 300  # 5 minutes

    # Regex patterns for various Mumble authenticator/server log lines
    PATTERNS = [
        # Alliance Auth / Python logging format:
        # [25/Oct/2026 19:31:50] INFO [mumble_authenticator:599] User authenticated: [FIGL] Vaximillian 1000000500
        # Authentication is the connect-side signal. Disconnects are normally
        # emitted by Murmur's server log, not by the authenticator callback.
        re.compile(
            r"^\[(?P<timestamp>\d{1,2}/[A-Za-z]{3}/\d{4}\s+\d{2}:\d{2}:\d{2})\]\s*"
            r"(?:\[?(?:INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\]?\s*)?"
            r"(?:\[(?P<logger>[^\]]+)\]\s*)?"
            r"User\s+authenticated\s*:\s*(?P<username>.+?)\s+(?P<session_id>\d+)\s*$",
            re.IGNORECASE,
        ),
        # 1. Bracketed timestamp / structured format:
        # [2026-10-22T20:30:15Z] Username AUTHENTICATE session=1234 channel="Fleet 1"
        # [2026-10-23 18:22:01] INFO [authenticator] userConnected: 12 (John Doe)
        re.compile(
            r"^\[(?P<timestamp>[^\]]+)\]\s*(?:\[?(?:INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\]?\s*)?(?:\[(?P<logger>[^\]]+)\]\s*)?(?:(?P<username>[^<>\(\)\s]+)\s+)?(?P<action>AUTHENTICATE|DISCONNECT|CHANNEL_MOVE|userConnected|userDisconnected|connected|disconnected)(?:\s*[:=]?\s*(?P<session_id>\d+))?(?:\s*\((?P<username2>[^\)]+)\))?(?:\s+session=(?P<session_id2>\S+))?(?:\s+channel=[\"'](?P<channel>[^\"']+)[\"'])?",
            re.IGNORECASE,
        ),
        # 2. Murmur native server log:
        # <W>2026-10-22 20:30:15.123 1 => <1234:Username(-1)> Authenticated
        # <W>2026-10-22 20:30:15.123 1 => <1234:Username(-1)> Connection closed: [-1]
        # <W>2026-10-22 20:30:15.123 1 => <1234:(-1)> Connection closed: [-1]
        re.compile(
            r"^<[A-Z]>(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\d+\s+=>\s+<(?P<session_id>\d+):(?:(?P<username>[^>\(]+)(?:\(-?\d+\))?|\(-?\d+\))>\s+(?P<action>Authenticated|Connection closed|Moved to channel:\s*(?P<channel>[^$]+)|[^\n]+)",
            re.IGNORECASE,
        ),
        # 3. Python Authenticator callback format:
        # 2026-10-23 18:22:01,123 - authenticator - INFO - userConnected: 12 (John Doe)
        # 2026-10-23 18:22:01,123 - authenticator - INFO - userDisconnected: 12 (John Doe)
        # 2026-10-23 18:22:01,123 - authenticator - INFO - userDisconnected: 12
        # 2026-10-23 18:22:01,123 - authenticator - INFO - userAuthenticate: John Doe -> 1
        # 2026-10-23 18:22:01,123 - authenticator - INFO - Authenticating user: John Doe
        # 2026-10-23 18:22:01,123 - authenticator - INFO - Authenticated John Doe
        re.compile(
            r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*(?:-\s*[\w\.]+\s*)?[-:]?\s*\[?(?:INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)?\]?\s*[-:]?\s*(?P<action>userConnected|userDisconnected|userAuthenticate|Authenticating user|Authenticated|Authenticate)\s*[:=]?\s*(?:session\s*(?P<session_id>\d+)\s*(?:for\s*)?)?(?:(?P<session_id_pre>\d+)\s*(?:\((?P<username>[^\)]+)\))?|['\"]?(?P<username2>[^'\"<>\n]+?)['\"]?(?:\s*->\s*(?P<session_id_post>\d+))?)$",
            re.IGNORECASE,
        ),
        # 4. Standard user connected / disconnected / channel move format:
        # 2026-10-22 20:30:15 - [INFO] User 'Username' connected (session 1234)
        # 2026-10-22 20:30:15,123 [INFO] User Username joined channel 'Fleet 1'
        # 2026-10-23 18:22:01,123 - authenticator - INFO - User John Doe moved to channel Fleet 1
        re.compile(
            r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}(?:[,\.]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\s*(?:-\s*[\w\.]+\s*)?[-:]?\s*\[?(?:INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)?\]?\s*[-:]?\s*(?:User\s+)?['\"]?(?P<username>[^'\"<>\n:]+?)['\"]?\s+(?P<action>connected|disconnected|joined channel\s+['\"]?(?P<channel>[^'\"]+)['\"]?|moved to channel\s*[:]?\s*['\"]?(?P<channel2>[^'\"]+)['\"]?)(?:.*session\s*[:=]?\s*(?P<session_id>\d+))?",
            re.IGNORECASE,
        ),
    ]

    @classmethod
    def parse_log_line(cls, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None

        for pattern in cls.PATTERNS:
            m = pattern.search(line)
            if m:
                data = m.groupdict()
                raw_ts = data["timestamp"].strip()
                parsed_dt = parse_datetime(raw_ts)
                if not parsed_dt:
                    try:
                        # Try standard formats
                        cleaned_ts = raw_ts.replace(",", ".")
                        parsed_dt = datetime.fromisoformat(cleaned_ts)
                    except Exception:
                        for fmt in (
                            "%d/%b/%Y %H:%M:%S",
                            "%Y-%m-%d %H:%M:%S.%f",
                            "%Y-%m-%d %H:%M:%S",
                            "%Y-%m-%dT%H:%M:%SZ",
                            "%Y-%m-%dT%H:%M:%S%z",
                        ):
                            try:
                                parsed_dt = datetime.strptime(
                                    raw_ts.replace(",", "."), fmt
                                )
                                break
                            except Exception:
                                pass
                if not parsed_dt:
                    continue
                if timezone.is_naive(parsed_dt):
                    parsed_dt = timezone.make_aware(parsed_dt)

                action_raw = data.get("action", "").lower()
                if "disconnect" in action_raw or "close" in action_raw:
                    action = "disconnect"
                elif "auth" in action_raw or "connect" in action_raw:
                    action = "connect"
                elif "channel" in action_raw or "move" in action_raw:
                    action = "channel_move"
                else:
                    action = "info"

                # Some Murmur/authenticator formats put the useful action in a
                # free-form message group. Preserve connection semantics even
                # when the regex matched the line through its broad fallback.
                if action == "info":
                    line_lower = line.lower()
                    if "disconnect" in line_lower or "connection closed" in line_lower:
                        action = "disconnect"
                    elif "authenticated" in line_lower or "userconnected" in line_lower:
                        action = "connect"
                    elif "connected" in line_lower:
                        action = "connect"
                    elif "channel" in line_lower or "moved" in line_lower:
                        action = "channel_move"

                username = (data.get("username") or data.get("username2") or "").strip()

                session_id = (
                    data.get("session_id")
                    or data.get("session_id_pre")
                    or data.get("session_id_post")
                    or data.get("session_id2")
                    or ""
                ).strip()

                channel = (data.get("channel") or data.get("channel2") or "").strip()

                return {
                    "timestamp": parsed_dt,
                    "username": username,
                    "action": action,
                    "session_id": session_id,
                    "channel": channel,
                }

        return None

    @classmethod
    def ingest_log_line(cls, line: str) -> CIEvent | None:
        parsed = cls.parse_log_line(line)
        if not parsed:
            return None

        return cls.ingest_raw_event(
            username=parsed["username"],
            action=parsed["action"],
            timestamp=parsed["timestamp"],
            channel_name=parsed.get("channel", ""),
            session_id=parsed.get("session_id", ""),
            metadata={"raw_line": line},
        )

    @classmethod
    def ingest_log_text(cls, text: str) -> list[CIEvent]:
        events = []
        for line in text.splitlines():
            ev = cls.ingest_log_line(line)
            if ev:
                events.append(ev)
        return events

    @classmethod
    def ingest_log_file(cls, file_path: str, use_offsets: bool = True) -> list[CIEvent]:
        """
        Reads and ingests a Mumble log file directly from disk idempotently
        using incremental seek offsets to minimize disk I/O and RAM usage.
        """
        # Standard Library
        import os

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
                start_offset = 0

        events = []
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                if start_offset > 0:
                    f.seek(start_offset)

                for line in f:
                    ev = cls.ingest_log_line(line)
                    if ev:
                        events.append(ev)

                if state:
                    state.last_offset = f.tell()
                    state.last_inode = current_inode
                    state.last_ingested_at = timezone.now()
                    state.save(
                        update_fields=["last_offset", "last_inode", "last_ingested_at"]
                    )
        except Exception:
            return []
        return events

    @classmethod
    def ingest_raw_event(
        cls,
        username: str,
        action: str,
        timestamp,
        channel_name: str = "",
        session_id: str = "",
        metadata: dict = None,
    ) -> CIEvent:
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)

        resolved_user = None
        if username:
            resolved_user = IdentityResolver.resolve_user_by_mumble(
                username, at_time=timestamp
            )

        # Fallback if username missing or not resolved directly (e.g. disconnect by session_id only)
        if not resolved_user and session_id:
            prev_event = (
                CIEvent.objects.filter(
                    source="mumble",
                    payload__session_id=str(session_id),
                )
                .exclude(user__isnull=True)
                .order_by("-timestamp")
                .first()
            )
            if prev_event:
                resolved_user = prev_event.user
                if not username and prev_event.payload:
                    username = prev_event.payload.get("username", "")

        source_event_id = f"mumble:{username or 'unknown'}:{session_id or '0'}:{action}:{timestamp.timestamp()}"
        payload = {
            "username": username,
            "action": action,
            "channel_name": channel_name,
            "session_id": str(session_id or ""),
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
                "user": resolved_user,
                "payload": payload,
            },
        )

        # Update or create session if user resolved and event was freshly created
        if created and resolved_user:
            cls._update_session(resolved_user, action, timestamp, channel_name)

        return event

    @classmethod
    def _update_session(cls, user, action: str, timestamp, channel_name: str = ""):
        if action == "connect":
            # Check for recent active or recently ended session within threshold (reconnect)
            cutoff = timestamp - timedelta(seconds=cls.RECONNECT_THRESHOLD_SECONDS)
            recent_session = (
                CISession.objects.filter(
                    user=user,
                    source="mumble",
                    start_time__lte=timestamp,
                )
                .order_by("-start_time")
                .first()
            )

            if (
                recent_session
                and recent_session.end_time
                and recent_session.end_time >= cutoff
            ):
                # Reconnection after brief drop
                recent_session.end_time = None
                recent_session.reconnect_count += 1
                if channel_name:
                    recent_session.channel_name = channel_name
                recent_session.save(
                    update_fields=["end_time", "reconnect_count", "channel_name"]
                )
            elif recent_session and not recent_session.end_time:
                # Session was already marked open
                if channel_name:
                    recent_session.channel_name = channel_name
                    recent_session.save(update_fields=["channel_name"])
            else:
                CISession.objects.create(
                    user=user,
                    source="mumble",
                    start_time=timestamp,
                    channel_name=channel_name,
                    reconnect_count=0,
                )

        elif action == "disconnect":
            active_session = (
                CISession.objects.filter(
                    user=user,
                    source="mumble",
                    end_time__isnull=True,
                    start_time__lte=timestamp,
                )
                .order_by("-start_time")
                .first()
            )
            if active_session:
                active_session.end_time = timestamp
                active_session.save(update_fields=["end_time"])

        elif action == "channel_move":
            active_session = (
                CISession.objects.filter(
                    user=user,
                    source="mumble",
                    end_time__isnull=True,
                    start_time__lte=timestamp,
                )
                .order_by("-start_time")
                .first()
            )
            if active_session:
                active_session.channel_name = channel_name
                active_session.save(update_fields=["channel_name"])
            else:
                CISession.objects.create(
                    user=user,
                    source="mumble",
                    start_time=timestamp,
                    channel_name=channel_name,
                    reconnect_count=0,
                )
