from .models import (
    BigBrotherConfig, RecurringStatsConfig
)
from .app_settings import send_message
from django.apps import apps

from celery import shared_task

import logging
logger = logging.getLogger(__name__)

@shared_task
def BB_send_recurring_stats():
    """
    Build and post recurring stats to the configured webhook.

    - Uses BigBrotherConfig for a webhook and schedule-enabled flag.
    - Uses RecurringStatsConfig for which states/stats to include and for deltas.
    """

    cfg = BigBrotherConfig.get_solo()

    webhook = cfg.stats_webhook
    if not webhook:
        logger.info("Recurring stats enabled but no stats_webhook configured; skipping.")
        return

    try:
        stats_cfg = RecurringStatsConfig.get_solo()
    except Exception:
        logger.warning("RecurringStatsConfig missing; cannot send recurring stats.")
        return

    if not stats_cfg.enabled:
        return

    from django.utils import timezone
    from allianceauth.authentication.models import UserProfile
    from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo, EveAllianceInfo

    # Helper for optional imports (Discord, Mumble, corptools, esi)
    def safe_import(path, name):
        try:
            module = __import__(path, fromlist=[name])
            return getattr(module, name)
        except Exception:
            return None

    discord_enabled = apps.is_installed("allianceauth.services.modules.discord")
    mumble_enabled = apps.is_installed("allianceauth.services.modules.mumble")

    DiscordUser = None
    MumbleUser = None

    if discord_enabled:
        DiscordUser = safe_import(
            "allianceauth.services.modules.discord.models", "DiscordUser"
        )

    if mumble_enabled:
        MumbleUser = safe_import(
            "allianceauth.services.modules.mumble.models", "MumbleUser"
        )

    Token = safe_import("esi.models", "Token")
    CharacterAudit = safe_import("corptools.models", "CharacterAudit")
    CorporationAudit = safe_import("corptools.models", "CorporationAudit")

    selected_states = list(stats_cfg.states.all())
    now = timezone.now()
    today = timezone.localdate()

    snapshot = {}

    # --- AUTH USERS ---
    if stats_cfg.include_auth_users:
        profiles_qs = UserProfile.objects.all()
        auth_total = profiles_qs.count()
        snapshot["auth_total"] = auth_total
        auth_by_state = {}
        for st in selected_states:
            auth_by_state[str(st.pk)] = profiles_qs.filter(state=st).count()
        snapshot["auth_by_state"] = auth_by_state

    # --- DISCORD USERS ---
    if stats_cfg.include_discord_users and DiscordUser is not None:
        dq = DiscordUser.objects.select_related("user__profile__state")
        discord_total = dq.count()
        snapshot["discord_total"] = discord_total
        discord_by_state = {}
        for st in selected_states:
            discord_by_state[str(st.pk)] = dq.filter(user__profile__state=st).count()
        snapshot["discord_by_state"] = discord_by_state

    # --- MUMBLE USERS ---
    if stats_cfg.include_mumble_users and MumbleUser is not None:
        mq = MumbleUser.objects.select_related("user__profile__state")
        mumble_total = mq.count()
        snapshot["mumble_total"] = mumble_total
        mumble_by_state = {}
        for st in selected_states:
            mumble_by_state[str(st.pk)] = mq.filter(user__profile__state=st).count()
        snapshot["mumble_by_state"] = mumble_by_state

    # --- EVE OBJECT COUNTS ---
    if stats_cfg.include_characters:
        snapshot["characters_total"] = EveCharacter.objects.count()

    if stats_cfg.include_corporations:
        snapshot["corporations_total"] = EveCorporationInfo.objects.count()

    if stats_cfg.include_alliances:
        snapshot["alliances_total"] = EveAllianceInfo.objects.count()

    # --- TOKENS ---
    if Token is not None:
        if stats_cfg.include_tokens:
            snapshot["tokens_total"] = Token.objects.count()
        if stats_cfg.include_unique_tokens:
            snapshot["tokens_unique"] = (
                Token.objects.values("character_id").distinct().count()
            )

    # --- AUDITS (corptools) ---
    if CharacterAudit is not None and stats_cfg.include_character_audits:
        snapshot["character_audits_total"] = CharacterAudit.objects.count()

    if CorporationAudit is not None and stats_cfg.include_corporation_audits:
        snapshot["corporation_audits_total"] = CorporationAudit.objects.count()

    # ---- DELTA CALCULATION ----
    previous = stats_cfg.last_snapshot or {}

    def fmt_delta(new, old):
        if old is None:
            return "+0"
        diff = new - old
        if diff > 0:
            return f"+{diff}"
        return str(diff)

    lines = []

    # Header with dates
    if stats_cfg.last_run_at:
        lines.append(f"{today.strftime('%m/%d')} (since {stats_cfg.last_run_at.date().isoformat()})")
    else:
        lines.append(today.strftime("%m/%d"))
    lines.append("")

    # AUTH USERS BLOCK
    if stats_cfg.include_auth_users and "auth_total" in snapshot:
        total = snapshot["auth_total"]
        prev_total = previous.get("auth_total")
        lines.append(f"{total} users on auth ({fmt_delta(total, prev_total)})")
        auth_by_state = snapshot.get("auth_by_state", {})
        prev_auth_by_state = previous.get("auth_by_state", {}) or {}
        for st in selected_states:
            cur = auth_by_state.get(str(st.pk), 0)
            old = prev_auth_by_state.get(str(st.pk))
            lines.append(f"- {st.name}: {cur} ({fmt_delta(cur, old)})")

        lines.append("")  # spacer

    # DISCORD BLOCK
    if stats_cfg.include_discord_users and "discord_total" in snapshot:
        total = snapshot["discord_total"]
        prev_total = previous.get("discord_total")
        lines.append(f"{total} users on discord ({fmt_delta(total, prev_total)})")
        discord_by_state = snapshot.get("discord_by_state", {})
        prev_discord_by_state = previous.get("discord_by_state", {}) or {}
        for st in selected_states:
            cur = discord_by_state.get(str(st.pk), 0)
            old = prev_discord_by_state.get(str(st.pk))
            lines.append(f"- {st.name}: {cur} ({fmt_delta(cur, old)})")

        lines.append("")

    # MUMBLE BLOCK
    if stats_cfg.include_mumble_users and "mumble_total" in snapshot:
        total = snapshot["mumble_total"]
        prev_total = previous.get("mumble_total")
        lines.append(f"{total} on mumble ({fmt_delta(total, prev_total)})")
        mumble_by_state = snapshot.get("mumble_by_state", {})
        prev_mumble_by_state = previous.get("mumble_by_state", {}) or {}
        for st in selected_states:
            cur = mumble_by_state.get(str(st.pk), 0)
            old = prev_mumble_by_state.get(str(st.pk))
            lines.append(f"- {st.name}: {cur} ({fmt_delta(cur, old)})")

        lines.append("")

    # FLAT COUNTS
    def add_flat(label, key):
        if key in snapshot:
            cur = snapshot[key]
            old = previous.get(key)
            lines.append(f"{label}: {cur} ({fmt_delta(cur, old)})")

    if any(
        [
            stats_cfg.include_characters,
            stats_cfg.include_corporations,
            stats_cfg.include_alliances,
            stats_cfg.include_tokens,
            stats_cfg.include_unique_tokens,
            stats_cfg.include_character_audits,
            stats_cfg.include_corporation_audits,
        ]
    ):
        lines.append("----")

    if stats_cfg.include_characters:
        add_flat("Characters", "characters_total")
    if stats_cfg.include_corporations:
        add_flat("Corporations", "corporations_total")
    if stats_cfg.include_alliances:
        add_flat("Alliances", "alliances_total")
    if stats_cfg.include_tokens:
        add_flat("Tokens", "tokens_total")
    if stats_cfg.include_unique_tokens:
        add_flat("Unique Tokens", "tokens_unique")
    if stats_cfg.include_character_audits:
        add_flat("Character Audits", "character_audits_total")
    if stats_cfg.include_corporation_audits:
        add_flat("Corporation Audits", "corporation_audits_total")

    message = "\n".join(lines).strip()
    if message:
        send_message(message, webhook)

    # Update snapshot + last run
    stats_cfg.last_run_at = now
    stats_cfg.last_snapshot = snapshot
    stats_cfg.save(update_fields=["last_run_at", "last_snapshot"])
