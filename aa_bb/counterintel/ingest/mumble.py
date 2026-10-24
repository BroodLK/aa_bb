"""
Idempotent Mumble log ingestor and session tracker.
"""

# Standard Library
from datetime import datetime, timedelta
import re

# Django
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from ..models import CIEvent, CISession
from ..services.identity import IdentityResolver


class MumbleLogIngestor:
    """
    Parses Mumble authenticator and server logs into normalized CIEvents
    and manages contiguous CISession records.
    """

    RECONNECT_THRESHOLD_SECONDS = 300  # 5 minutes

    # Regex patterns for various Mumble authenticator/server log lines
    PATTERNS = [
        # <W>2026-10-22 20:30:15.123 1 => <1234:Username(-1)> Authenticated
        re.compile(
            r"<[A-Z]>(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+\d+\s+=>\s+<(?P<session_id>\d+):(?P<username>[^>\(]+)(?:\(-?\d+\))?>\s+(?P<action>Authenticated|Connection closed|Moved to channel:\s*(?P<channel>[^$]+))",
            re.IGNORECASE,
        ),
        # 2026-10-22 20:30:15 - [INFO] User 'Username' connected (session 1234)
        re.compile(
            r"(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*-\s*\[?[A-Z]+\]?\s*User\s+'(?P<username>[^']+)'\s+(?P<action>connected|disconnected|joined channel\s+'(?P<channel>[^']+)')(?:.*session\s+(?P<session_id>\d+))?",
            re.IGNORECASE,
        ),
        # [2026-10-22T20:30:15Z] Username AUTHENTICATE session=1234 channel="Fleet 1"
        re.compile(
            r"\[(?P<timestamp>[^\]]+)\]\s+(?P<username>\S+)\s+(?P<action>AUTHENTICATE|DISCONNECT|CHANNEL_MOVE)(?:\s+session=(?P<session_id>\S+))?(?:\s+channel=\"(?P<channel>[^\"]+)\")?",
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
                        continue
                if parsed_dt and timezone.is_naive(parsed_dt):
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

                return {
                    "timestamp": parsed_dt,
                    "username": data["username"].strip(),
                    "action": action,
                    "session_id": (data.get("session_id") or "").strip(),
                    "channel": (data.get("channel") or "").strip(),
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
    def ingest_log_file(cls, file_path: str) -> list[CIEvent]:
        """
        Reads and ingests a Mumble log file directly from disk idempotently.
        """
        import os

        if not os.path.exists(file_path):
            return []

        events = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    ev = cls.ingest_log_line(line)
                    if ev:
                        events.append(ev)
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

        resolved_user = IdentityResolver.resolve_user_by_mumble(username, at_time=timestamp)
        source_event_id = f"mumble:{username}:{session_id or '0'}:{action}:{timestamp.timestamp()}"
        payload = {
            "username": username,
            "action": action,
            "channel_name": channel_name,
            "session_id": session_id,
            "metadata": metadata or {},
        }
        dedup_hash = CIEvent.generate_dedup_hash("mumble", source_event_id, timestamp, payload)

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

            if recent_session and recent_session.end_time and recent_session.end_time >= cutoff:
                # Reconnection after brief drop
                recent_session.end_time = None
                recent_session.reconnect_count += 1
                if channel_name:
                    recent_session.channel_name = channel_name
                recent_session.save(update_fields=["end_time", "reconnect_count", "channel_name"])
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
