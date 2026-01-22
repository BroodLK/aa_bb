"""
Celery tasks backing the CorpBrother module and related utilities.

This file contains:
  • `CB_run_regular_updates` which rebuilds every corp’s cache entries.
  • Compliance helper tasks (role/token checking, PAP gaps, EveWho audits).
  • Daily/optional Discord message broadcasters and their schedule bootstrapper.
  • LoA status checks and DB cleanup routines.
"""

import time
import traceback
import random
import gc
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

from django.utils import timezone
from django.db.models import Q
from celery import shared_task
from django_celery_beat.models import PeriodicTask, CrontabSchedule, IntervalSchedule

from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from allianceauth.authentication.models import UserProfile

from .models import (
    BigBrotherConfig, CorpStatus, Messages, OptMessages1, OptMessages2, OptMessages3,
    OptMessages4, OptMessages5
)
from .tasks_utils import setup_periodic_task
from .models import (
    ProcessedContract,
    SusContractNote,
    ProcessedMail,
    SusMailNote,
    ProcessedTransaction,
    SusTransactionNote,
    PapCompliance,
    LeaveRequest
)
from .app_settings import (
    get_pings,
    resolve_corporation_name,
    get_users,
    get_user_id,
    get_character_id,
    get_user_profiles,
    send_status_embed,
    _chunk_embed_lines,
    corptools_active,
)
from aa_bb.checks_cb.hostile_assets import get_corp_hostile_asset_locations
from aa_bb.checks_cb.sus_contracts import get_corp_hostile_contracts
from aa_bb.checks_cb.sus_trans import get_corp_hostile_transactions
from aa_bb.checks.roles_and_tokens import get_user_roles_and_tokens

try:
    if corptools_active():
        from corptools.api.helpers import get_alts_queryset
        from corptools.models import (
            Contract,
            MailMessage,
            CorporateContract,
            CharacterWalletJournalEntry,
            CorporationWalletJournalEntry,
        )
    else:
        get_alts_queryset = None
        Contract = None
        MailMessage = None
        CorporateContract = None
        CharacterWalletJournalEntry = None
        CorporationWalletJournalEntry = None
except ImportError:
    get_alts_queryset = None
    Contract = None
    MailMessage = None
    CorporateContract = None
    CharacterWalletJournalEntry = None
    CorporationWalletJournalEntry = None

from django.db import transaction, OperationalError, close_old_connections, connection
from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)
VERBOSE_WEBHOOK_LOGGING = True




@shared_task(time_limit=7200)
def CB_send_discord_notifications(subject: str, chunks: list[list[str]]) -> None:
    """
    Dedicated task to send Discord embeds for CorpBrother.

    - subject: usually the corp name
    - chunks: list of "lines lists" – each inner list becomes one embed body
    """
    close_old_connections()

    logger.info(
        "✅  [AA-BB] - [CB_send_discord_notifications] - Dispatching %d embed chunks for %s",
        len(chunks),
        subject,
    )

    for idx, lines in enumerate(chunks):
        logger.debug(
            "✅  [AA-BB] - [CB_send_discord_notifications] - Sending chunk %d/%d for %s (lines=%d)",
            idx + 1,
            len(chunks),
            subject,
            len(lines),
        )
        send_status_embed(
            subject=subject,
            lines=lines,
            override_title="",  # keep titles minimal; content is in the body
        )
        time.sleep(0.25)  # tiny delay to be nice to the webhook

    # Clean up after sending all chunks
    del chunks
    gc.collect()
    close_old_connections()


@shared_task(time_limit=7200)
def CB_update_single_corp(corp_id):
    """
    Process updates for a single corporation.
    """
    # Close old DB connections to prevent memory leaks
    close_old_connections()

    instance = BigBrotherConfig.get_solo()
    if not instance.is_active:
        close_old_connections()
        return

    # Resolve corp name if missing
    corpstatus, created = CorpStatus.objects.get_or_create(corp_id=corp_id)
    if not corpstatus.corp_name:
        corpstatus.corp_name = resolve_corporation_name(corp_id)
    corp_name = corpstatus.corp_name

    for attempt in range(3):
        try:
            ignored_str = instance.ignored_corporations or ""
            ignored_ids = {int(s) for s in ignored_str.split(",") if s.strip().isdigit()}
            if corp_id in ignored_ids:
                return

            hostile_assets_result = get_corp_hostile_asset_locations(corp_id)
            sus_contracts_result = {str(issuer_id): v for issuer_id, v in get_corp_hostile_contracts(corp_id).items()}
            sus_trans_result = {str(issuer_id): v for issuer_id, v in get_corp_hostile_transactions(corp_id).items()}

            has_hostile_assets = bool(hostile_assets_result)
            has_sus_contracts = bool(sus_contracts_result)
            has_sus_trans = bool(sus_trans_result)

            corp_changes = []

            def as_dict(x):
                return x if isinstance(x, dict) else {}

            # Hostile Assets
            if corpstatus.has_hostile_assets != has_hostile_assets or set(hostile_assets_result) != set(corpstatus.hostile_assets or []):
                old_systems = set(corpstatus.hostile_assets or [])
                new_systems = set(hostile_assets_result) - old_systems

                asset_lines = []
                for system in sorted(new_systems):
                    data = hostile_assets_result.get(system)
                    if not data or not isinstance(data, dict):
                        continue

                    owner = data.get("owner", "Unresolvable")
                    region = data.get("region", "Unknown Region")
                    records = data.get("records", [])

                    asset_lines.append(f"- {system} ({owner} | Region: {region})")
                    for rec in records:
                        loc_name = rec.get("location_name", "Unknown Location")
                        asset_lines.append(f"   - {loc_name}")

                link_list = "\n".join(asset_lines)
                logger.debug(f"✅  [AA-BB] - [CB_update_single_corp] - {corp_name} new assets {link_list}")
                if corpstatus.has_hostile_assets != has_hostile_assets:
                    if not has_hostile_assets:
                        corp_changes.append(f"## Hostile Corp Assets: 🟢")

                if asset_lines:
                    corp_changes.append(f"##{get_pings('New Hostile Assets')} New Hostile Assets:\n{link_list}")

                corpstatus.has_hostile_assets = has_hostile_assets
                corpstatus.hostile_assets = hostile_assets_result

            # Suspicious Contracts
            if corpstatus.has_sus_contracts != has_sus_contracts or set(sus_contracts_result) != set(as_dict(corpstatus.sus_contracts) or {}):
                old_contracts = as_dict(corpstatus.sus_contracts) or {}
                old_ids = set(old_contracts.keys())
                new_ids = set(sus_contracts_result.keys())
                new_links = new_ids - old_ids

                if corpstatus.has_sus_contracts != has_sus_contracts:
                    if not has_sus_contracts:
                        corp_changes.append(f"## Sus Corp Contracts: 🟢")

                if new_links:
                    contract_lines = []
                    for issuer_id in sorted(new_links):
                        res = sus_contracts_result[issuer_id]
                        ping = get_pings('New Sus Contracts')
                        if res.startswith("- A -"):
                            ping = ""
                        contract_lines.append(f"{res} {ping}")

                    if contract_lines:
                        corp_changes.append(f"## New Sus Contracts:\n" + "\n".join(contract_lines))

                corpstatus.has_sus_contracts = has_sus_contracts
                corpstatus.sus_contracts = sus_contracts_result

            # Suspicious Transactions
            if corpstatus.has_sus_trans != has_sus_trans or set(sus_trans_result) != set(as_dict(corpstatus.sus_trans) or {}):
                old_trans = as_dict(corpstatus.sus_trans) or {}
                old_ids = set(old_trans.keys())
                new_ids = set(sus_trans_result.keys())
                new_links = new_ids - old_ids

                if corpstatus.has_sus_trans != has_sus_trans:
                    if not has_sus_trans:
                        corp_changes.append(f"## Sus Corp Transactions: 🟢")

                if new_links:
                    link_list_tx = "\n".join(f"{sus_trans_result[issuer_id]}" for issuer_id in sorted(new_links))
                    corp_changes.append(f"### New Sus Transactions{get_pings('New Sus Transactions')}:\n{link_list_tx}")

                corpstatus.has_sus_trans = has_sus_trans
                corpstatus.sus_trans = sus_trans_result

            # Notifications
            if not corpstatus.baseline_initialized:
                send_notifications = instance.new_user_notify
            else:
                send_notifications = True

            if send_notifications and corp_changes:
                all_lines: list[str] = []
                for block in corp_changes:
                    all_lines.extend(str(block).split("\n"))

                all_chunks = []
                header_lines = [f"‼️ Status change detected for {corp_name}"]
                for header_chunk in _chunk_embed_lines(header_lines, max_chars=1900):
                    all_chunks.append(header_chunk)

                chunks = _chunk_embed_lines(all_lines, max_chars=1900)
                for chunk in chunks:
                    all_chunks.append(chunk)

                if all_chunks:
                    logger.info(
                        "✅  [AA-BB] - [CB_update_single_corp] - [%s] Enqueuing %d embed chunks to CB_send_discord_notifications",
                        corp_name,
                        len(all_chunks),
                    )
                    CB_send_discord_notifications.delay(corp_name, all_chunks)

            corpstatus.baseline_initialized = True
            corpstatus.updated = timezone.now()
            corpstatus.save()

            # Clean up large variables to free memory
            del hostile_assets_result, sus_contracts_result, sus_trans_result, corp_changes
            if 'all_chunks' in locals():
                del all_chunks

            # Force garbage collection and close connections
            gc.collect()
            close_old_connections()
            break

        except OperationalError as e:
            code = e.args[0] if e.args else None
            if code == 1213 or "deadlock" in str(e).lower():
                delay = 0.5 * (attempt + 1)
                logger.warning(
                    f"ℹ️  [AA-BB] - [CB_update_single_corp] - Deadlock while processing {corp_id} "
                    f"(attempt {attempt + 1}/3); sleeping {delay:.1f}s before retry."
                )
                time.sleep(delay)
                if attempt == 2:
                    logger.error(f"ℹ️  [AA-BB] - [CB_update_single_corp] - Skipping {corp_id} after repeated deadlocks.")
                continue
            raise
        except Exception as e:
            logger.error(f"ℹ️  [AA-BB] - [CB_update_single_corp] - Failed to update corp {corp_id}: {e}", exc_info=True)
            # Clean up on error
            gc.collect()
            close_old_connections()
            raise


@shared_task(time_limit=7200)
def CB_run_regular_updates():
    """
    Update CorpBrother caches: hostile assets, contracts, transactions, LoA, and PAPs.
    """
    # Close old DB connections to prevent memory leaks
    close_old_connections()

    instance = BigBrotherConfig.get_solo()
    instance.refresh_from_db()

    try:
        if instance.is_active:
            qs = EveCorporationInfo.objects.all()
            if qs is None:
                return

            member_corps = {int(x) for x in (instance.member_corporations or "").split(",") if x.strip().isdigit()}
            member_allis = {int(x) for x in (instance.member_alliances or "").split(",") if x.strip().isdigit()}
            if member_corps or member_allis:
                qs = qs.filter(Q(corporation_id__in=member_corps) | Q(alliance_id__in=member_allis))

            corps = list(
                qs.select_related('alliance')
                .values_list("corporation_id", flat=True)
                .order_by("corporation_name")
                .filter(corporationaudit__isnull=False)
            )

            total_corps = len(corps)
            logger.info(f"✅  [AA-BB] - [CB_run_regular_updates] - Dispatching updates for {total_corps} corps.")

            # Backlog check (cached to avoid repeated expensive introspection)
            try:
                from django.core.cache import cache
                backlog_cache_key = "cb_update_backlog_check"
                remaining_count = cache.get(backlog_cache_key)

                if remaining_count is None:
                    from celery import current_app
                    inspector = current_app.control.inspect()
                if remaining_count is None and inspector:
                    active = inspector.active() or {}
                    reserved = inspector.reserved() or {}
                    task_name = CB_update_single_corp.name
                    remaining_count = 0

                    for worker_tasks in active.values():
                        if worker_tasks:
                            for t in worker_tasks:
                                if t.get('name') == task_name:
                                    remaining_count += 1
                    for worker_tasks in reserved.values():
                        if worker_tasks:
                            for t in worker_tasks:
                                if t.get('name') == task_name:
                                    remaining_count += 1

                    # Cache for 60 seconds to avoid repeated expensive introspection
                    cache.set(backlog_cache_key, remaining_count, 60)

                if remaining_count and remaining_count > 0 and instance.update_backlog_notify:
                    # For corps we might use the same threshold or a fixed one?
                    # Let's use the same threshold but against total_corps?
                    # Actually, let's just use a fixed small number or similar logic.
                    if total_corps > 0:
                        percent = (remaining_count / total_corps) * 100
                        if percent > instance.update_backlog_threshold:
                            logger.warning(f"ℹ️  [AA-BB] - [CB_run_regular_updates] - Corp Update backlog detected: {remaining_count} tasks remaining")
                            send_status_embed(
                                subject="Corp Update Backlog Alert",
                                lines=[f"{get_pings('Error')} {remaining_count} corps are still being processed from the previous run."],
                                color=0xFF0000,
                            )
            except Exception as e:
                logger.error(f"ℹ️  [AA-BB] - [CB_run_regular_updates] - Failed to check for backlog: {e}")

            # Dispatch with staggered delays to smooth load
            for idx, corp_id in enumerate(corps):
                CB_update_single_corp.apply_async(args=[corp_id], countdown=idx * 0.5)

            # Clean up corp list
            del corps
        else:
            logger.warning("ℹ️  [AA-BB] - [CB_run_regular_updates] - Plugin is disabled (is_active=False), skipping corp updates.")

        # Force garbage collection and close connections
        gc.collect()
        close_old_connections()

    except Exception as e:
        logger.error("ℹ️  [AA-BB] - [CB_run_regular_updates] - Task failed", exc_info=True)
        gc.collect()
        close_old_connections()
        tb_str = traceback.format_exc()
        tb_lines = [f"{get_pings('Error')} Corp Brother encountered an unexpected error", "```python"] + tb_str.split("\n") + ["```"]
        for chunk in _chunk_embed_lines(tb_lines):
            send_status_embed(
                subject="Corp Brother Error",
                lines=chunk,
                color=0xFF0000,
            )

    from .tasks_utils import format_task_name
    task_name = format_task_name('CB run regular updates')
    task = PeriodicTask.objects.filter(name=task_name).first()
    if task and not task.enabled:
        send_status_embed(
            subject="Corp Brother",
            lines=["Task has finished, you can now enable the task"],
            color=0x00FF00,
        )


@shared_task(time_limit=7200)
def check_member_compliance():
    """
    Nightly compliance sweep:
      • Ensures characters with corp roles still have valid corp tokens.
      • Reports missing characters per corp/alliance (via EveWho).
      • Sends a single consolidated Discord message with all findings.
    """
    # Close old DB connections to prevent memory leaks
    close_old_connections()

    instance = BigBrotherConfig.get_solo()
    instance.refresh_from_db()
    if not instance.is_active:  # plugin disabled → skip expensive checks
        logger.warning("ℹ️  [AA-BB] - [check_member_compliance] - Plugin is disabled (is_active=False), skipping compliance sweep.")
        close_old_connections()
        return
    profiles_qs = get_user_profiles()
    if instance.limit_to_main_corp:
        profiles_qs = profiles_qs.filter(main_character__corporation_id=instance.main_corporation_id)

    users = list(profiles_qs.values_list("main_character__character_name", flat=True))
    messages = ""

    for char_name in users:
        user_id = get_user_id(char_name)
        data = get_user_roles_and_tokens(user_id)
        flags = ""

        for character, info in data.items():
            has_roles = any(info.get(role, False) for role in ("director", "accountant", "station_manager", "personnel_manager"))
            has_char_token = info.get("character_token", False)
            has_corp_token = info.get("corporation_token", False)

            # Non-compliant if character has roles but no corporation token or missing character token
            if not has_char_token or (has_roles and not has_corp_token):  # only flag when a requirement is unmet
                details = []
                if not has_char_token:  # Missing personal token always fails compliance.
                    details.append("      - missing character token\n")
                if has_roles and not has_corp_token:  # Corp roles mandate a corp token.
                    details.append("      - has corp roles but missing corp token\n")
                flags += f"  - {character}:\n{''.join(details)}"

        if flags:  # append per-user block when at least one character was non-compliant
            messages += f"-  {char_name}:\n{flags}"

    from allianceauth.eveonline.models import EveCorporationInfo, EveCharacter
    from .app_settings import get_corporation_info, get_alliance_name
    missing_characters = []

    if instance.limit_to_main_corp:
        corp_ids = str(instance.main_corporation_id)
        ali_ids = ""
    else:
        corp_ids = instance.member_corporations
        ali_ids = instance.member_alliances

    if corp_ids:  # optionally check extra corp ids even if they're outside auth
        # Batch query all corp characters upfront
        all_corp_ids = [int(c.strip()) for c in corp_ids.split(",") if c.strip().isdigit()]
        linked_chars_by_corp = {}
        if all_corp_ids:
            all_linked = EveCharacter.objects.filter(
                corporation_id__in=all_corp_ids
            ).values('corporation_id', 'character_name')
            for rec in all_linked:
                linked_chars_by_corp.setdefault(rec['corporation_id'], set()).add(rec['character_name'])

        for corp_id in all_corp_ids:
            corp_chars = []
            linked_chars = linked_chars_by_corp.get(corp_id, set())

            corp_name = get_corporation_info(corp_id)["name"]
            # Get characters from EveWho API
            all_corp_members = get_corp_character_names(corp_id)
            # Find missing characters
            for char_name in all_corp_members:
                if char_name not in linked_chars:  # not linked in Auth → report
                    corp_chars.append(f"  - {char_name}")
            if corp_chars:  # Only append corp section when missing members were found.
                chars_str = "\n".join(corp_chars)
                missing_characters.append(f"- {corp_name}\n{chars_str}")

    logger.debug(f"✅  [AA-BB] - [check_member_compliance] - ali_ids: {str(ali_ids)}")
    if ali_ids:  # optional alliance-level audits
        # Batch query all alliance characters upfront
        all_ali_ids = [int(a.strip()) for a in ali_ids.split(",") if a.strip().isdigit()]
        linked_chars_by_ali = {}
        if all_ali_ids:
            all_linked = EveCharacter.objects.filter(
                alliance_id__in=all_ali_ids
            ).values('alliance_id', 'character_name')
            for rec in all_linked:
                linked_chars_by_ali.setdefault(rec['alliance_id'], set()).add(rec['character_name'])

        for ali_id in all_ali_ids:
            logger.debug(f"✅  [AA-BB] - [check_member_compliance] - ali_id: {str(ali_id)}")
            ali_chars = []
            linked_chars = linked_chars_by_ali.get(ali_id, set())
            logger.debug(f"✅  [AA-BB] - [check_member_compliance] - linked_chars: {str(linked_chars)}")

            ali_name = get_alliance_name(ali_id)
            logger.debug(f"✅  [AA-BB] - [check_member_compliance] - ali_name: {str(ali_name)}")
            # Get characters from EveWho API
            all_ali_members = get_ali_character_names(ali_id)
            logger.debug(f"✅  [AA-BB] - [check_member_compliance] - all_ali_members: {str(all_ali_members)}")
            # Find missing characters
            for char_name in all_ali_members:
                if char_name not in linked_chars:  # missing from Auth → flag
                    ali_chars.append(f"  - {char_name}")
            if ali_chars:  # Only add block when missing alliance characters exist.
                chars_str = "\n".join(ali_chars)
                missing_characters.append(f"- {ali_name}\n{chars_str}")
    compliance_msg = ""
    if missing_characters:  # Prepend EveWho gaps when any exist.
        logger.debug(f"✅  [AA-BB] - [check_member_compliance] - missing_characters: {str(missing_characters)}")
        joined_msg = '\n'.join(missing_characters)
        compliance_msg += f"\n## Missing tokens for member characters:\n{joined_msg}"

    if messages:  # Attach per-user compliance flags when collected.
        compliance_msg += f"\n## Non Compliant users found:\n" + messages

    if compliance_msg:  # Only ping Discord when there is something to report.
        lines = [f"{get_pings('Compliance')} Compliance Issues found:"] + compliance_msg.split("\n")
        for chunk in _chunk_embed_lines(lines):
            send_status_embed(
                subject="Compliance Audit",
                lines=chunk,
                color=0xFF0000,
            )

    # Clean up large data structures
    if 'missing_characters' in locals():
        del missing_characters
    if 'users' in locals():
        del users
    gc.collect()
    close_old_connections()

import requests

def get_corp_character_names(corp_id: int) -> str:
    """Return the full list of member names for `corp_id` via EveWho."""
    time.sleep(3.5)
    url = f"https://evewho.com/api/corplist/{corp_id}"
    headers = {
        "User-Agent": "AllianceAuth-CorpBrother"
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    return [char["name"] for char in data.get("characters", [])]

def get_ali_character_names(ali_id: int) -> str:
    """Return the full list of member names for `ali_id` via EveWho."""
    time.sleep(3.5)
    url = f"https://evewho.com/api/allilist/{ali_id}"
    headers = {
        "User-Agent": "AllianceAuth-CorpBrother"
    }
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    return [char["name"] for char in data.get("characters", [])]


@shared_task(time_limit=7200)
def BB_send_daily_messages():
    """Send one random daily message to the configured webhook each run."""
    config = BigBrotherConfig.get_solo()
    webhook = config.dailywebhook
    enabled = config.are_daily_messages_active

    if not enabled:  # admin paused the feed
        return

    # Get only messages not sent in this cycle
    unsent_messages = Messages.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # cycle exhausted → reset send flags
        # Reset all messages if cycle is complete
        Messages.objects.update(sent_in_cycle=False)
        unsent_messages = Messages.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # still nothing → nothing to do
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Daily Message",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task(time_limit=7200)
def BB_send_opt_message1():
    """Send one optional message #1 if enabled"""
    config = BigBrotherConfig.get_solo()
    webhook = config.optwebhook1
    enabled = config.are_opt_messages1_active

    if not enabled:  # Admin paused this message stream.
        return

    # Get only messages not sent in this cycle
    unsent_messages = OptMessages1.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Cycle complete; reset send flags.
        # Reset all messages if cycle is complete
        OptMessages1.objects.update(sent_in_cycle=False)
        unsent_messages = OptMessages1.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Still nothing available; exit quietly.
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Optional Message #1",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task(time_limit=7200)
def BB_send_opt_message2():
    """Optional message stream #2."""
    config = BigBrotherConfig.get_solo()
    webhook = config.optwebhook2
    enabled = config.are_opt_messages2_active

    if not enabled:  # Admin disabled this stream.
        return

    # Get only messages not sent in this cycle
    unsent_messages = OptMessages2.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Reset once the batch is exhausted.
        # Reset all messages if cycle is complete
        OptMessages2.objects.update(sent_in_cycle=False)
        unsent_messages = OptMessages2.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Nothing to send after reset.
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Optional Message #2",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task(time_limit=7200)
def BB_send_opt_message3():
    """Optional message stream #3."""
    config = BigBrotherConfig.get_solo()
    webhook = config.optwebhook3
    enabled = config.are_opt_messages3_active

    if not enabled:  # Stream disabled by admin.
        return

    # Get only messages not sent in this cycle
    unsent_messages = OptMessages3.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Reset cycle to reuse messages.
        # Reset all messages if cycle is complete
        OptMessages3.objects.update(sent_in_cycle=False)
        unsent_messages = OptMessages3.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Nothing left after reset.
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Optional Message #3",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task(time_limit=7200)
def BB_send_opt_message4():
    """Optional message stream #4."""
    config = BigBrotherConfig.get_solo()
    webhook = config.optwebhook4
    enabled = config.are_opt_messages4_active

    if not enabled:  # Stream disabled by admin.
        return

    # Get only messages not sent in this cycle
    unsent_messages = OptMessages4.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Reset cycle to prepare new run.
        # Reset all messages if cycle is complete
        OptMessages4.objects.update(sent_in_cycle=False)
        unsent_messages = OptMessages4.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Nothing available even after reset.
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Optional Message #4",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task(time_limit=7200)
def BB_send_opt_message5():
    """Optional message stream #5."""
    config = BigBrotherConfig.get_solo()
    webhook = config.optwebhook5
    enabled = config.are_opt_messages5_active

    if not enabled:  # Stream paused in admin UI.
        return

    # Get only messages not sent in this cycle
    unsent_messages = OptMessages5.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Reset flags when everyone sent.
        # Reset all messages if cycle is complete
        OptMessages5.objects.update(sent_in_cycle=False)
        unsent_messages = OptMessages5.objects.filter(sent_in_cycle=False)

    if not unsent_messages.exists():  # Nothing to do after reset.
        return  # Still nothing to send

    message = random.choice(list(unsent_messages))
    send_status_embed(
        subject="Optional Message #5",
        lines=[message.text],
        color=0x3498db,
        hook=webhook
    )

    # Mark as sent
    message.sent_in_cycle = True
    message.save()


@shared_task(time_limit=7200)
def BB_register_message_tasks():
    """
    Ensure all periodic tasks exist and match the configuration.
    """
    logger.info("✅  [AA-BB] - [BB_register_message_tasks] - Running BB_register_message_tasks...")
    from .tasks_utils import sync_periodic_tasks
    sync_periodic_tasks()


@shared_task(time_limit=7200)
def BB_run_regular_loa_updates():
    """
    Scan every member main and update LoA statuses / inactivity flags.
    """
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_active:
        logger.warning("ℹ️  [AA-BB] - [BB_run_regular_loa_updates] - Plugin is disabled.")
        return
    qs_profiles = get_user_profiles()
    if not qs_profiles.exists():  # No members matching filters, so nothing to process.
        logger.info("ℹ️  [AA-BB] - [BB_run_regular_loa_updates] - No member mains found.")
        return

    flags = []

    for profile in qs_profiles:
        user = profile.user
        # Determine main_character_id
        try:
            main_id = profile.main_character.character_id
        except Exception:
            main_id = get_character_id(profile)

        # Load main character
        ec = EveCharacter.objects.filter(character_id=main_id).first()
        if not ec:  # Skip mains that cannot be resolved to an EveCharacter.
            continue

        # Find the most recent logoff among all alts
        latest_logoff = None
        for char in get_alts_queryset(ec):
            audit = getattr(char, "characteraudit", None)
            ts = getattr(audit, "last_known_logoff", None) if audit else None
            if ts and (latest_logoff is None or ts > latest_logoff):  # Track the most recent logoff across alts.
                latest_logoff = ts

        if not latest_logoff:  # Without logoff data inactivity cannot be determined.
            continue

        # Compute days since that logoff
        days_since = (timezone.now() - latest_logoff).days

        # 1) Check and update any existing approved requests for this user
        lr_qs = LeaveRequest.objects.filter(
            user=user,
        )
        today = timezone.localdate()
        for lr in lr_qs:
            if lr.start_date <= today <= lr.end_date and lr.status == "approved":  # Approved LoAs become in-progress when dates hit.
                lr.status = "in_progress"
                lr.save(update_fields=["status"])
                send_status_embed(
                    subject="LoA Status Change",
                    lines=[f"{user.username}'s LoA Request status changed to in progress"],
                    color=0x3498db,
                )
            elif today > lr.end_date and lr.status != "finished":  # Auto-close requests whose end dates passed.
                lr.status = "finished"
                lr.save(update_fields=["status"])
                send_status_embed(
                    subject="LoA Finished",
                    lines=[
                        f"{get_pings('LoA Changed Status')} **{ec}**'s LoA",
                        f"- from **{lr.start_date}**",
                        f"- to **{lr.end_date}**",
                        f"- for **{lr.reason}**",
                        "## has finished"
                    ],
                    color=0x3498db,
                )
        has_active_loa = LeaveRequest.objects.filter(
            user=user,
            status="in_progress",
            start_date__lte=today,
            end_date__gte=today,
        ).exists()
        if days_since > cfg.loa_max_logoff_days and not has_active_loa:  # Flag members inactive beyond policy without LoA.
            flags.append(f"- **{ec}** was last seen online on {latest_logoff} (**{days_since}** days ago where maximum w/o a LoA request is **{cfg.loa_max_logoff_days}**)")
    if flags and cfg.is_loa_active:  # Notify staff when inactivity breaches are detected. but also don't send unless LOA is actually on
        lines = [f"{get_pings('LoA Inactivity')} Inactive Members Found:"] + flags
        for chunk in _chunk_embed_lines(lines):
            send_status_embed(
                subject="LoA Inactivity",
                lines=chunk,
                color=0xFF0000,
            )


@shared_task(time_limit=7200)
def BB_daily_DB_cleanup():
    """
    Periodic cleanup of cached tables and orphaned processed records.

    Deletes stale name caches, employment caches, processed mail/contract/transaction
    entries that no longer have backing data, and non-member PAP compliance rows.
    """
    close_old_connections()
    if not BigBrotherConfig.get_solo().is_active:
        logger.warning("ℹ️  [AA-BB] - [BB_daily_DB_cleanup] - Plugin is disabled (is_active=False), skipping DB cleanup.")
        return
    from .models import (
        Alliance_names, Character_names, Corporation_names, UserStatus, EntityInfoCache,
        id_types, CharacterEmploymentCache, FrequentCorpChangesCache, CurrentStintCache, AwoxKillsCache,
        CorporationInfoCache, AllianceHistoryCache, SovereigntyMapCache
    )

    prune_threshold = timezone.now() - timedelta(days=30)
    flags = []
    # Delete high-churn EntityInfoCache separately using raw SQL for performance
    try:
        threshold_entity = timezone.now() - timedelta(days=2)
        with connection.cursor() as cursor:
            table_name = EntityInfoCache._meta.db_table
            total_deleted = 0
            batch_size = 10000
            vendor = connection.vendor
            while True:
                if vendor in ['mysql', 'mariadb']:
                    cursor.execute(f"DELETE FROM {table_name} WHERE updated < %s LIMIT %s", [threshold_entity, batch_size])
                    count = cursor.rowcount
                elif vendor == 'postgresql':
                    cursor.execute(f"DELETE FROM {table_name} WHERE id IN (SELECT id FROM {table_name} WHERE updated < %s LIMIT %s)", [threshold_entity, batch_size])
                    count = cursor.rowcount
                else:
                    cursor.execute(f"DELETE FROM {table_name} WHERE updated < %s", [threshold_entity])
                    count = cursor.rowcount

                total_deleted += count
                if count < batch_size or vendor not in ['mysql', 'mariadb', 'postgresql']:
                    break
                time.sleep(0.1)

            if total_deleted > 0:
                flags.append(f"- Deleted {total_deleted} old Entity Info Cache records.")
        close_old_connections()
    except Exception as e:
        logger.error(f"Failed to cleanup EntityInfoCache: {e}")

    # Delete other old model entries using ORM
    models_to_cleanup = [
        (Alliance_names, "alliance"),
        (Character_names, "character"),
        (Corporation_names, "corporation"),
        (UserStatus, "User Status"),
        (CorporationInfoCache, "Corporation Info Cache"),
        (AllianceHistoryCache, "Alliance History Cache"),
        (SovereigntyMapCache, "Sovereignty Map Cache"),
    ]

    for model, name in models_to_cleanup:
        threshold = prune_threshold
        old_entries = model.objects.filter(updated__lt=threshold)
        count, _ = old_entries.delete()
        if count > 0:
            flags.append(f"- Deleted {count} old {name} records.")

    # Cleanup caches using last_accessed
    last_access_models = [
        (CharacterEmploymentCache, "Character Employment Cache"),
        (FrequentCorpChangesCache, "Frequent Corp Changes Cache"),
        (CurrentStintCache, "Current Stint Cache"),
        (AwoxKillsCache, "AWOX Kills Cache"),
    ]
    for model, name in last_access_models:
        try:
            old_entries = model.objects.filter(last_accessed__lt=prune_threshold)
            count, _ = old_entries.delete()
            if count > 0:
                flags.append(f"- Deleted {count} old {name} records (by last access).")
        except Exception:
            continue

    # id_types: delete if not looked up in last 30 days
    try:
        stale_ids = id_types.objects.filter(last_accessed__lt=prune_threshold)
        count, _ = stale_ids.delete()
        if count > 0:
            flags.append(f"- Deleted {count} old ID type cache records (by last access).")
    except Exception:
        pass

    # -- CONTRACTS --
    # Use database-side subqueries to avoid loading all IDs into memory
    from django.db.models import Q, Exists, OuterRef

    # Find ProcessedContracts with no matching source record (memory-efficient)
    orphaned_processed_contracts = ProcessedContract.objects.filter(
        ~Exists(CorporateContract.objects.filter(contract_id=OuterRef('contract_id'))) &
        ~Exists(Contract.objects.filter(contract_id=OuterRef('contract_id')))
    )

    # Get count before deletion for reporting
    count_proc = orphaned_processed_contracts.count()

    if count_proc > 0:
        # Delete related SusContractNotes and ProcessedContracts
        with transaction.atomic():
            count_sus = SusContractNote.objects.filter(
                contract__in=orphaned_processed_contracts
            ).delete()[0]
            orphaned_processed_contracts.delete()

        flags.append(f"- Deleted {count_proc} old ProcessedContract and {count_sus} SusContractNote records.")

    # -- MAILS --
    # Use database-side subquery to avoid loading all mail IDs into memory
    orphaned_processed_mails = ProcessedMail.objects.filter(
        ~Exists(MailMessage.objects.filter(id_key=OuterRef('mail_id')))
    )

    count_proc = orphaned_processed_mails.count()

    if count_proc > 0:
        with transaction.atomic():
            count_sus = SusMailNote.objects.filter(
                mail__in=orphaned_processed_mails
            ).delete()[0]
            orphaned_processed_mails.delete()

        flags.append(f"- Deleted {count_proc} old ProcessedMail and {count_sus} SusMailNote records.")

    # -- TRANSACTIONS --
    # Use database-side subquery to avoid loading all transaction IDs into memory
    orphaned_processed_transactions = ProcessedTransaction.objects.filter(
        ~Exists(CharacterWalletJournalEntry.objects.filter(entry_id=OuterRef('entry_id'))) &
        ~Exists(CorporationWalletJournalEntry.objects.filter(entry_id=OuterRef('entry_id')))
    )

    count_proc = orphaned_processed_transactions.count()

    if count_proc > 0:
        with transaction.atomic():
            count_sus = SusTransactionNote.objects.filter(
                transaction__in=orphaned_processed_transactions
            ).delete()[0]
            orphaned_processed_transactions.delete()

        flags.append(f"- Deleted {count_proc} old ProcessedTransaction and {count_sus} SusTransactionNote records.")

    # -- PAP COMPLIANCE: drop entries for non-members --
    try:
        # Use database-side subquery to avoid loading all profile IDs into memory
        non_member_pc_qs = PapCompliance.objects.filter(
            ~Exists(get_user_profiles().filter(id=OuterRef('user_profile_id')))
        )
        deleted_pc = non_member_pc_qs.delete()[0]
        if deleted_pc > 0:
            flags.append(f"- Deleted {deleted_pc} PapCompliance records for non-members.")
    except Exception as e:
        logger.warning(f"ℹ️  [AA-BB] - [BB_daily_DB_cleanup] - PapCompliance cleanup failed: {e}")

    if flags:  # Summarize cleanup actions when anything was removed.
        lines = ["DB Cleanup Complete:"] + flags
        for chunk in _chunk_embed_lines(lines):
            send_status_embed(
                subject="DB Cleanup",
                lines=chunk,
                color=0x3498db,
            )
