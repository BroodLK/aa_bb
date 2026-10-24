"""
Identity resolution service supporting effective dates across EVE, Discord, Mumble, and Auth.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils import timezone

from ..models import CIIdentity


class IdentityResolver:
    """
    Resolves identities to Alliance Auth User objects retroactively or currently,
    ensuring username changes, unlinking, or character transfers are respected
    based on historical effective date ranges.
    """

    @classmethod
    def resolve_user_by_character(cls, character_id: int, at_time=None) -> User | None:
        if not character_id:
            return None
        dt = at_time or timezone.now()
        qs = CIIdentity.objects.filter(
            character_id=character_id,
        ).filter(
            Q(effective_from__lte=dt) | Q(effective_from__lte=dt + timedelta(minutes=5))
        ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=dt))
        identity = qs.select_related("user").order_by("-is_verified", "-effective_from").first()
        if identity:
            return identity.user

        # Fallback to direct CharacterOwnership if within current time
        try:
            from allianceauth.authentication.models import CharacterOwnership

            co = (
                CharacterOwnership.objects.filter(character__character_id=character_id)
                .select_related("user")
                .first()
            )
            if co and co.user:
                return co.user
        except Exception:
            pass

        return None

    @classmethod
    def resolve_user_by_discord(cls, discord_id: int, at_time=None) -> User | None:
        if not discord_id:
            return None
        dt = at_time or timezone.now()
        qs = CIIdentity.objects.filter(
            discord_id=discord_id,
            effective_from__lte=dt,
        ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=dt))
        identity = qs.select_related("user").order_by("-is_verified", "-effective_from").first()
        if identity:
            return identity.user

        # Fallback to DiscordUser
        try:
            from allianceauth.services.modules.discord.models import DiscordUser

            du = DiscordUser.objects.filter(uid=discord_id).select_related("user").first()
            if du and du.user:
                return du.user
        except Exception:
            pass

        return None

    @classmethod
    def resolve_user_by_mumble(cls, mumble_username: str, at_time=None) -> User | None:
        if not mumble_username:
            return None
        clean_name = mumble_username.strip()
        dt = at_time or timezone.now()
        qs = CIIdentity.objects.filter(
            mumble_username__iexact=clean_name,
            effective_from__lte=dt,
        ).filter(Q(effective_until__isnull=True) | Q(effective_until__gt=dt))
        identity = qs.select_related("user").order_by("-is_verified", "-effective_from").first()
        if identity:
            return identity.user

        # Fallback: check matching Auth username or main character name
        user = User.objects.filter(username__iexact=clean_name).first()
        if user:
            return user

        try:
            from allianceauth.eveonline.models import EveCharacter

            char = EveCharacter.objects.filter(character_name__iexact=clean_name).first()
            if char:
                return cls.resolve_user_by_character(char.character_id, at_time=dt)
        except Exception:
            pass

        return None

    @classmethod
    def register_identity(
        cls,
        user: User,
        character_id: int = None,
        discord_id: int = None,
        mumble_username: str = None,
        effective_from=None,
        effective_until=None,
        is_verified: bool = False,
    ) -> CIIdentity:
        eff_from = effective_from or timezone.now()
        identity, _ = CIIdentity.objects.get_or_create(
            user=user,
            character_id=character_id,
            discord_id=discord_id,
            mumble_username=mumble_username,
            effective_from=eff_from,
            defaults={
                "effective_until": effective_until,
                "is_verified": is_verified,
            },
        )
        if effective_until is not None:
            identity.effective_until = effective_until
            identity.save(update_fields=["effective_until"])
        return identity

    @classmethod
    def retire_identity(cls, identity: CIIdentity, effective_until=None) -> CIIdentity:
        identity.effective_until = effective_until or timezone.now()
        identity.save(update_fields=["effective_until"])
        return identity

    @classmethod
    def sync_user_identities(cls, user: User, at_time=None):
        """
        Scan user's characters and external services to maintain CIIdentity records.
        """
        eff_from = at_time or timezone.now()
        # Characters
        try:
            from allianceauth.authentication.models import CharacterOwnership

            for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
                cls.register_identity(
                    user=user,
                    character_id=co.character.character_id,
                    effective_from=eff_from,
                    is_verified=True,
                )
        except Exception:
            pass

        # Discord
        try:
            from allianceauth.services.modules.discord.models import DiscordUser

            du = DiscordUser.objects.filter(user=user).first()
            if du and du.uid:
                cls.register_identity(
                    user=user,
                    discord_id=int(du.uid),
                    effective_from=eff_from,
                    is_verified=True,
                )
        except Exception:
            pass

        # Mumble default username (typically Auth username or main char name)
        cls.register_identity(
            user=user,
            mumble_username=user.username,
            effective_from=eff_from,
            is_verified=False,
        )
        profile = getattr(user, "profile", None)
        if profile and profile.main_character:
            cls.register_identity(
                user=user,
                mumble_username=profile.main_character.character_name,
                effective_from=eff_from,
                is_verified=False,
            )
