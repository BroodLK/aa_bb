"""Ingest BigBrother's existing EVE activity snapshots into Counterintel."""

# Django
from django.contrib.auth import get_user_model
from django.utils import timezone

from ..models import CIEvent


class EveActivityIngestor:
    """Normalize cached character logoff data without creating tickets."""

    @classmethod
    def ingest_latest_logoffs(cls) -> int:
        """Record newly observed character logoff snapshots.

        BigBrother/CorpTools already refreshes CharacterAudit data during
        normal member updates. Counterintel consumes that cache and never
        invokes ticketing.
        """
        try:
            # Third Party
            from corptools.api.helpers import get_alts_queryset

            # Alliance Auth
            from allianceauth.eveonline.models import EveCharacter
        except ImportError:
            return 0

        User = get_user_model()
        created = 0
        users = User.objects.filter(profile__main_character__isnull=False).distinct()
        for user in users.iterator():
            main = user.profile.main_character
            try:
                characters = get_alts_queryset(main).select_related("characteraudit")
            except (AttributeError, TypeError):
                characters = EveCharacter.objects.filter(character_id=main.character_id)

            for character in characters:
                audit = getattr(character, "characteraudit", None)
                timestamp = getattr(audit, "last_known_logoff", None) if audit else None
                if not timestamp:
                    continue
                if timezone.is_naive(timestamp):
                    timestamp = timezone.make_aware(timestamp)

                source_event_id = (
                    f"eve:logoff:{character.character_id}:{timestamp.isoformat()}"
                )
                payload = {
                    "character_id": character.character_id,
                    "character_name": character.character_name,
                    "activity_type": "logoff",
                }
                _, was_created = CIEvent.objects.get_or_create(
                    dedup_hash=CIEvent.generate_dedup_hash(
                        "eve_activity", source_event_id, timestamp, payload
                    ),
                    defaults={
                        "source": "eve_activity",
                        "source_event_id": source_event_id,
                        "timestamp": timestamp,
                        "user": user,
                        "payload": payload,
                    },
                )
                created += int(was_created)
        return created
