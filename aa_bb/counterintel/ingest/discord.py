"""
Idempotent Discord voice presence and activity ingestor.
"""

# Standard Library
from datetime import timedelta

# Django
from django.utils import timezone

from ..models import CIEvent, CISession
from ..services.identity import IdentityResolver


class DiscordIngestor:
    """
    Ingests Discord voice and communication metadata idempotently,
    tracking voice channel sessions while respecting privacy by never storing message text.
    """

    RECONNECT_THRESHOLD_SECONDS = 180  # 3 minutes

    @classmethod
    def ingest_voice_state_update(
        cls,
        discord_user_id: int,
        channel_id: str | int = "",
        channel_name: str = "",
        action: str = "join",  # join, leave, move, mute, deafen
        timestamp=None,
        mute: bool = False,
        deaf: bool = False,
    ) -> CIEvent:
        ts = timestamp or timezone.now()
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)

        user = IdentityResolver.resolve_user_by_discord(discord_user_id, at_time=ts)
        source_event_id = (
            f"discord:voice:{discord_user_id}:{channel_id}:{action}:{ts.timestamp()}"
        )
        payload = {
            "discord_user_id": discord_user_id,
            "channel_id": str(channel_id),
            "channel_name": channel_name,
            "action": action,
            "mute": mute,
            "deaf": deaf,
        }
        dedup_hash = CIEvent.generate_dedup_hash(
            "discord", source_event_id, ts, payload
        )

        event, created = CIEvent.objects.get_or_create(
            dedup_hash=dedup_hash,
            defaults={
                "source": "discord",
                "source_event_id": source_event_id,
                "timestamp": ts,
                "user": user,
                "payload": payload,
            },
        )

        if created and user and action in ("join", "leave", "move"):
            cls._update_voice_session(user, action, ts, channel_name)

        return event

    @classmethod
    def ingest_message_event(
        cls,
        discord_user_id: int,
        channel_id: str | int,
        message_id: str | int,
        timestamp=None,
        content_length: int = 0,
    ) -> CIEvent:
        ts = timestamp or timezone.now()
        if timezone.is_naive(ts):
            ts = timezone.make_aware(ts)

        user = IdentityResolver.resolve_user_by_discord(discord_user_id, at_time=ts)
        source_event_id = f"discord:msg:{discord_user_id}:{channel_id}:{message_id}"
        # Privacy guardrail: store only event metadata and length, no message content
        payload = {
            "discord_user_id": discord_user_id,
            "channel_id": str(channel_id),
            "message_id": str(message_id),
            "content_length": content_length,
        }
        dedup_hash = CIEvent.generate_dedup_hash(
            "discord", source_event_id, ts, payload
        )

        event, _ = CIEvent.objects.get_or_create(
            dedup_hash=dedup_hash,
            defaults={
                "source": "discord",
                "source_event_id": source_event_id,
                "timestamp": ts,
                "user": user,
                "payload": payload,
            },
        )

        if user:
            try:
                # AA BigBrother
                from aa_bb.models import UserStatus

                UserStatus.objects.update_or_create(
                    user=user,
                    defaults={"last_discord_message_at": ts},
                )
            except Exception:
                pass

        return event

    @classmethod
    def _update_voice_session(
        cls, user, action: str, timestamp, channel_name: str = ""
    ):
        if action == "join":
            cutoff = timestamp - timedelta(seconds=cls.RECONNECT_THRESHOLD_SECONDS)
            recent_session = (
                CISession.objects.filter(
                    user=user,
                    source="discord",
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
                recent_session.end_time = None
                recent_session.reconnect_count += 1
                if channel_name:
                    recent_session.channel_name = channel_name
                recent_session.save(
                    update_fields=["end_time", "reconnect_count", "channel_name"]
                )
            elif recent_session and not recent_session.end_time:
                if channel_name:
                    recent_session.channel_name = channel_name
                    recent_session.save(update_fields=["channel_name"])
            else:
                CISession.objects.create(
                    user=user,
                    source="discord",
                    start_time=timestamp,
                    channel_name=channel_name,
                    reconnect_count=0,
                )

        elif action == "leave":
            active_session = (
                CISession.objects.filter(
                    user=user,
                    source="discord",
                    end_time__isnull=True,
                    start_time__lte=timestamp,
                )
                .order_by("-start_time")
                .first()
            )
            if active_session:
                active_session.end_time = timestamp
                active_session.save(update_fields=["end_time"])

        elif action == "move":
            active_session = (
                CISession.objects.filter(
                    user=user,
                    source="discord",
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
                    source="discord",
                    start_time=timestamp,
                    channel_name=channel_name,
                    reconnect_count=0,
                )
