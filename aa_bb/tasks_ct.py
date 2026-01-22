"""
Corptools integration tasks (CT = Corptools).

These helpers mirror the upstream corptools modules but add BigBrother-specific
logging, throttling, and message notifications.
"""

import datetime
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence
from celery import shared_task, chain
from random import random

from django.utils import timezone
from django.db.models import Q
from celery_once.tasks import AlreadyQueued

from allianceauth.services.hooks import get_extension_logger
from esi.errors import TokenExpiredError, TokenError, TokenInvalidError
from esi.models import Token

from .app_settings import send_message, send_status_embed, _chunk_embed_lines, corptools_active
from .models import BigBrotherConfig

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import EveCharacter
        from corptools import app_settings
        from corptools.models import CharacterAudit, CorptoolsConfiguration

        from corptools.tasks.character import (
            update_char_assets,
            update_char_contacts,
            update_char_notifications,
            update_char_roles,
            update_char_titles,
            update_char_mining_ledger,
            update_char_wallet,
            update_char_transactions,
            update_char_orders,
            update_char_order_history,
            update_char_contracts,
            update_char_skill_list,
            update_char_skill_queue,
            update_char_clones,
            update_char_mail,
            update_char_loyaltypoints,
            update_char_industry_jobs,
            update_char_location,
        )
    else:
        CharacterAudit = None
except ImportError:
    CharacterAudit = None


@dataclass(frozen=True)
class ModuleRule:
    """Definition of a CT module, its gating attributes, and tasks to execute."""

    name: str
    enabled_flag: Optional[str]
    runtime_disable_attr: Optional[str]
    last_update_fields: Sequence[str]
    required_scopes: Sequence[str]
    tasks: Sequence[Callable]
    extra_predicate: Optional[Callable[[], bool]] = None


def _safe_identity_refresh(char_id: int):
    """Ensure the EveCharacter identity cache is refreshed without exploding. Cached per session."""
    from django.core.cache import cache
    cache_key = f"aa_bb_identity_refresh_{char_id}"

    # Check if we've already refreshed this character recently (cache for 1 hour)
    if cache.get(cache_key):
        return

    try:
        EveCharacter.objects.update_character(char_id)
        cache.set(cache_key, True, 3600)  # Cache for 1 hour
    except Exception as e:
        logger.warning(f"✅  [AA-BB] - [_safe_identity_refresh] - Identity refresh failed for {char_id}: {e}", exc_info=True)


def _is_enabled(flag_name: Optional[str]) -> bool:
    """Check whether a module flag is globally enabled."""
    if not flag_name:  # No flag configured, treat module as enabled by default.
        return True
    return bool(getattr(app_settings, flag_name, False))


def _not_temp_disabled(conf: CorptoolsConfiguration, attr: Optional[str]) -> bool:
    """Inspect runtime toggle on CorptoolsConfiguration to see if module paused."""
    if not attr:  # No runtime toggle specified, nothing to disable here.
        return True
    return bool(getattr(conf, attr, False) is False)


def _available_fields(audit: CharacterAudit, fields: Iterable[str]) -> List[str]:
    """Return the subset of last-update fields that the audit record actually has."""
    return [f for f in fields if hasattr(audit, f)]


def _is_stale_value(dt, cutoff) -> bool:
    """Determine whether a timestamp is missing or older than the cutoff."""
    return (dt is None) or (dt <= cutoff)


def _any_available_field_stale(audit: CharacterAudit, fields: Iterable[str], cutoff) -> bool:
    """Check whether any available last-update field is stale."""
    avail = _available_fields(audit, fields)
    if not avail:  # If audit lacks these fields, the entry is not considered stale.
        return False
    return any(_is_stale_value(getattr(audit, f, None), cutoff) for f in avail)


def _has_valid_token_with_scopes(char_id: int, scopes: Sequence[str]) -> bool:
    """Confirm the character has a valid token with the requested scopes."""
    if not scopes:  # Some modules have no scope requirement (e.g. local caches).
        return True
    token = Token.get_token(char_id, scopes)
    if not token:  # Cannot run tasks without a token satisfying required scopes.
        return False
    try:
        return bool(token.valid_access_token())
    except (TokenExpiredError, TokenInvalidError) as e:
        logger.info(f"✅  [AA-BB] - [_has_valid_token_with_scopes] - Skipping char {char_id}: unusable token for scopes {scopes} ({e.__class__.__name__})")
        return False
    except Exception as e:
        logger.warning(f"✅  [AA-BB] - [_has_valid_token_with_scopes] - Unexpected token error for char {char_id} (scopes {scopes}): {e}", exc_info=True)
        return False



RULES: List[ModuleRule] = [
    ModuleRule(
        name="Location",
        enabled_flag="CT_CHAR_LOCATIONS_MODULE",
        runtime_disable_attr="disable_update_location",
        last_update_fields=["last_update_location", "last_update_locations"],
        required_scopes=["esi-location.read_location.v1", "esi-location.read_ship_type.v1"],
        tasks=[update_char_location],
    ),
    ModuleRule(
        name="Assets",
        enabled_flag="CT_CHAR_ASSETS_MODULE",
        runtime_disable_attr="disable_update_assets",
        last_update_fields=["last_update_assets"],
        required_scopes=["esi-assets.read_assets.v1"],
        tasks=[update_char_assets],
    ),
    ModuleRule(
        name="Contacts",
        enabled_flag="CT_CHAR_CONTACTS_MODULE",
        runtime_disable_attr="disable_update_contacts",
        last_update_fields=["last_update_contacts"],
        required_scopes=["esi-characters.read_contacts.v1"],
        tasks=[update_char_contacts],
    ),
    ModuleRule(
        name="Notifications",
        enabled_flag="CT_CHAR_NOTIFICATIONS_MODULE",
        runtime_disable_attr="disable_update_notif",
        last_update_fields=["last_update_notif"],
        required_scopes=["esi-characters.read_notifications.v1"],
        tasks=[update_char_notifications],
    ),
    ModuleRule(
        name="Roles/Titles",
        enabled_flag="CT_CHAR_ROLES_MODULE",
        runtime_disable_attr="disable_update_roles",
        last_update_fields=["last_update_roles", "last_update_titles"],
        required_scopes=[
            "esi-characters.read_titles.v1",
            "esi-characters.read_corporation_roles.v1",
        ],
        tasks=[update_char_roles, update_char_titles],
    ),
    ModuleRule(
        name="Industry",
        enabled_flag="CT_CHAR_INDUSTRY_MODULE",
        runtime_disable_attr="disable_update_indy",
        last_update_fields=["last_update_indy"],
        required_scopes=["esi-industry.read_character_jobs.v1"],
        tasks=[update_char_industry_jobs],
    ),
    ModuleRule(
        name="Mining",
        enabled_flag="CT_CHAR_MINING_MODULE",
        runtime_disable_attr="disable_update_mining",
        last_update_fields=["last_update_mining"],
        required_scopes=["esi-industry.read_character_mining.v1"],
        tasks=[update_char_mining_ledger],
    ),
    ModuleRule(
        name="Wallet/Markets",
        enabled_flag="CT_CHAR_WALLET_MODULE",
        runtime_disable_attr="disable_update_wallet",
        last_update_fields=["last_update_wallet", "last_update_orders"],
        required_scopes=[
            "esi-wallet.read_character_wallet.v1",
            "esi-markets.read_character_orders.v1",
        ],
        tasks=[update_char_wallet, update_char_transactions, update_char_orders, update_char_order_history],
    ),
    ModuleRule(
        name="Contracts",
        enabled_flag="CT_CHAR_WALLET_MODULE",
        runtime_disable_attr="disable_update_wallet",
        last_update_fields=["last_update_contracts"],
        required_scopes=["esi-contracts.read_character_contracts.v1"],
        tasks=[update_char_contracts],
        extra_predicate=lambda: (not getattr(app_settings, "CT_CHAR_PAUSE_CONTRACTS", False)),
    ),
    ModuleRule(
        name="Clones",
        enabled_flag="CT_CHAR_CLONES_MODULE",
        runtime_disable_attr="disable_update_clones",
        last_update_fields=["last_update_clones"],
        required_scopes=[
            "esi-clones.read_clones.v1",
            "esi-clones.read_implants.v1",
        ],
        tasks=[update_char_clones],
    ),
    ModuleRule(
        name="Mail",
        enabled_flag="CT_CHAR_MAIL_MODULE",
        runtime_disable_attr="disable_update_mails",
        last_update_fields=["last_update_mails"],
        required_scopes=["esi-mail.read_mail.v1"],
        tasks=[update_char_mail],
    ),
    ModuleRule(
        name="Loyalty Points",
        enabled_flag="CT_CHAR_LOYALTYPOINTS_MODULE",
        runtime_disable_attr="disable_update_loyaltypoints",
        last_update_fields=["last_update_loyaltypoints"],
        required_scopes=["esi-characters.read_loyalty.v1"],
        tasks=[update_char_loyaltypoints],
    ),
    ModuleRule(
        name="Skills",
        enabled_flag="CT_CHAR_SKILLS_MODULE",
        runtime_disable_attr="disable_update_skills",
        last_update_fields=["last_update_skills", "last_update_skill_que"],
        required_scopes=[
            "esi-skills.read_skills.v1",
            "esi-skills.read_skillqueue.v1",
        ],
        tasks=[update_char_skill_list, update_char_skill_queue],
    ),
]


@shared_task(time_limit=7200)
def kickstart_stale_ct_modules(days_stale: int = 2, limit: Optional[int] = None, dry_run: bool = False) -> str:
    """
    Iterate audits and queue CT module tasks for any characters with stale data.

    Args:
        days_stale: Age threshold for stale module data.
        limit: Optional cap on number of audits to inspect.
        dry_run: When True, log intended actions instead of enqueueing.

    Returns:
        Summary string announcing what was queued (and optionally posted to chat).
    """
    if not corptools_active() or CharacterAudit is None:
        logger.error("✅  [AA-BB] - [kickstart_stale_ct_modules] - Corptools not installed or models missing, skipping.")
        return "Corptools not installed or models missing"
    instance = BigBrotherConfig.get_solo()
    instance.refresh_from_db()
    if not instance.is_active:
        return "Big Brother is inactive."
    conf = CorptoolsConfiguration.get_solo()
    cutoff = timezone.now() - datetime.timedelta(days=days_stale)
    cutoff_really_stale = timezone.now() - datetime.timedelta(days=days_stale, hours=6)

    qs = CharacterAudit.objects.filter(
        character__character_ownership__isnull=False
    ).select_related("character")

    member_corps = {int(x) for x in (instance.member_corporations or "").split(",") if x.strip().isdigit()}
    member_allis = {int(x) for x in (instance.member_alliances or "").split(",") if x.strip().isdigit()}
    if member_corps or member_allis:
        qs = qs.filter(Q(character__corporation_id__in=member_corps) | Q(character__alliance_id__in=member_allis))

    if limit:  # Honor caller-provided limit to avoid scanning the entire table.
        qs = qs[: int(limit)]

    total_chars = 0
    total_tasks = 0
    submitted_chars = 0
    updated_names: List[str] = []

    for audit in qs.iterator():
        total_chars += 1
        char_id = audit.character.character_id
        kickedcharactermodel = False

        que: List = []

        for rule in RULES:
            really_stale = _any_available_field_stale(audit, rule.last_update_fields, cutoff_really_stale)
            if not _is_enabled(rule.enabled_flag):  # Skip modules turned off in settings.
                continue
            if not _not_temp_disabled(conf, rule.runtime_disable_attr):  # Honor runtime pause switches.
                continue
            if rule.extra_predicate and not rule.extra_predicate():  # Allow ad-hoc guards (e.g. paused contracts).
                continue
            if not _has_valid_token_with_scopes(char_id, rule.required_scopes):  # Cannot update without the scopes.
                continue
            if not _any_available_field_stale(audit, rule.last_update_fields, cutoff):  # Fresh data needs no action.
                continue

            if not kickedcharactermodel and really_stale and not dry_run:  # Refresh identity once for badly stale chars.
                _safe_identity_refresh(char_id)
                kickedcharactermodel = True

            for task in rule.tasks:
                sig = task.si(char_id, force_refresh=True).set(once={'graceful': True})
                que.append(sig)
                total_tasks += 1

        if que:  # Only proceed if at least one CT module task was assembled.
            updated_names.append(audit.character.character_name)

            if dry_run:  # Only log the plan—do not enqueue tasks in dry-run mode.
                logger.info(
                    f"✅  [AA-BB] - [kickstart_stale_ct_modules] - [DRY-RUN] Would submit chain of {len(que)} task(s) "
                    f"for {audit.character.character_name} ({char_id})"
                )
            else:
                delay = random() * getattr(app_settings, "CT_TASK_SPREAD_DELAY", 0)
                try:
                    chain(*que).apply_async(priority=6, countdown=max(0, int(delay)))
                    submitted_chars += 1
                    logger.info(
                        "✅  [AA-BB] - [kickstart_stale_ct_modules] - Queued chain of %d task(s) for %s (%s) with delay=%s",
                        len(que),
                        audit.character.character_name,
                        char_id,
                        int(delay),
                    )
                except AlreadyQueued as e:
                    logger.info("✅  [AA-BB] - [kickstart_stale_ct_modules] - Skipped chain for %s (%s): first task already queued (ttl≈%s)",
                                audit.character.character_name, char_id, getattr(e, 'args', [None])[0])

    # Build summary + optional message
    if updated_names:  # Send a digest if something was queued so staff gets visibility.
        names_str = ", ".join(updated_names)
        summary = (
            f"## CT audit complete:\n"
            f"- Processed {total_chars} characters\n"
            f"- Queued {total_tasks} module task(s) across {submitted_chars} character(s) (stale > {days_stale}d).\n"
            f"- Characters queued:\n{names_str}"
        )
    else:
        summary = (
            f"## CT audit complete:\n"
            f"- Processed {total_chars} characters\n"
            f"- No stale modules found (threshold > {days_stale}d)."
        )

    ct_update_notify = BigBrotherConfig.get_solo().ct_notify
    if ct_update_notify and updated_names:
        lines = summary.split("\n")
        chunks = _chunk_embed_lines(lines)
        for chunk in chunks:
            send_status_embed(
                subject="CT Audit Update",
                lines=chunk,
                color=0x9b59b6,  # Purple
            )

    return summary
