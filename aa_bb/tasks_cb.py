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
from datetime import timedelta
import logging

logger = logging.getLogger(__name__)

from django.utils import timezone
from celery import shared_task
from django_celery_beat.models import PeriodicTask, CrontabSchedule

from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from allianceauth.authentication.models import UserProfile

from .models import (
    BigBrotherConfig, CorpStatus, Messages, OptMessages1, OptMessages2, OptMessages3,
    OptMessages4, OptMessages5
)
from .models import (
    ProcessedContract, SusContractNote,
    ProcessedMail, SusMailNote,
    ProcessedTransaction, SusTransactionNote,
)

from .app_settings import send_message, get_pings, resolve_corporation_name, get_users, get_user_id, get_character_id, get_user_profiles
from aa_bb.checks_cb.hostile_assets import get_corp_hostile_asset_locations
from aa_bb.checks_cb.sus_contracts import get_corp_hostile_contracts
from aa_bb.checks_cb.sus_trans import get_corp_hostile_transactions
from aa_bb.checks.roles_and_tokens import get_user_roles_and_tokens

try:
    from corptools.api.helpers import get_alts_queryset
    from corptools.models import (
        Contract,
        MailMessage,
        CorporateContract,
        CharacterWalletJournalEntry,
        CorporationWalletJournalEntry,
    )
except ImportError:
    logger.error("corptools not installed, CB tasks will not be available.")
from .modelss import PapCompliance, LeaveRequest



from django.db import transaction
@shared_task
def CB_run_regular_updates():
    """
    Update CorpBrother caches: hostile assets, contracts, transactions, LoA, and PAPs.

    Steps:
      1. Abort immediately if the DLC is not enabled.
      2. Fetch corporation ids with an audit record, skipping ignored corpids.
      3. Refresh or create `CorpStatus` rows and emit Discord messages when new
         hostile signals are detected.
      4. Catch errors, disable the task, and stream the traceback to Discord.
    """
    instance = BigBrotherConfig.get_solo()

    if not instance.dlc_corp_brother_active:  # DLC not purchased → skip entirely
        logger.info("Corp Brother DLC disabled; skipping CB_run_regular_updates.")
        return

    try:
        if instance.is_active:  # only run when the install is active/licensed
            # Corp Brother
            qs = EveCorporationInfo.objects.all()
            corps = []
            if qs is not None:  # Skip the queryset handling entirely when no corporations exist.
                corps = (
                    qs.values_list("corporation_id", flat=True)
                      .order_by("corporation_name")
                ).filter(
                    corporationaudit__isnull=False,
                )


            for corp_id in corps:
                ignored_str = BigBrotherConfig.get_solo().ignored_corporations or ""
                ignored_ids = {int(s) for s in ignored_str.split(",") if s.strip().isdigit()}
                if corp_id in ignored_ids:  # allow admins to hide certain corps entirely
                    continue
                hostile_assets_result = get_corp_hostile_asset_locations(corp_id)
                sus_contracts_result = { str(issuer_id): v for issuer_id, v in get_corp_hostile_contracts(corp_id).items() }
                sus_trans_result = { str(issuer_id): v for issuer_id, v in get_corp_hostile_transactions(corp_id).items() }

                has_hostile_assets = bool(hostile_assets_result)
                has_sus_contracts = bool(sus_contracts_result)
                has_sus_trans = bool(sus_trans_result)

                # Load or create existing record
                corpstatus, created = CorpStatus.objects.get_or_create(corp_id=corp_id)

                corp_changes = []

                #corpstatus.hostile_assets = []
                #corpstatus.sus_contracts = {}
                #corpstatus.sus_trans = {}
                def as_dict(x):
                    """Return dicts for JSON fields while tolerating None/strings."""
                    return x if isinstance(x, dict) else {}

                if not corpstatus.corp_name:  # Resolve names on first run to avoid API hits later.
                    corpstatus.corp_name = resolve_corporation_name(corp_id)

                corp_name = corpstatus.corp_name

                if corpstatus.has_hostile_assets != has_hostile_assets or set(hostile_assets_result) != set(corpstatus.hostile_assets or []):  # hostile asset list changed?
                    # Compare and find new links
                    old_links = set(corpstatus.hostile_assets or [])
                    new_links = set(hostile_assets_result) - old_links
                    link_list = "\n".join(
                        f"- {system} owned by {hostile_assets_result[system]}"
                        for system in (set(hostile_assets_result) - set(corpstatus.hostile_assets or []))
                    )
                    logger.info(f"{corp_name} new assets {link_list}")
                    link_list2 = "\n- ".join(f"🔗 {link}" for link in old_links)
                    logger.info(f"{corp_name} old assets {link_list2}")
                    if corpstatus.has_hostile_assets != has_hostile_assets:  # summarize boolean change
                        corp_changes.append(f"## Hostile Assets: {'🚩' if has_hostile_assets else '✖'}")
                        logger.info(f"{corp_name} changed")
                    if new_links:  # announce newly detected systems
                        corp_changes.append(f"##{get_pings('New Hostile Assets')} New Hostile Assets:\n{link_list}")
                        logger.info(f"{corp_name} new assets")
                    corpstatus.has_hostile_assets = has_hostile_assets
                    corpstatus.hostile_assets = hostile_assets_result

                if corpstatus.has_sus_contracts != has_sus_contracts or set(sus_contracts_result) != set(as_dict(corpstatus.sus_contracts) or {}):  # Rebuild block when contract list changed.
                    old_contracts = as_dict(corpstatus.sus_contracts) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(corpstatus.sus_contracts).keys())
                    new_ids   = set(sus_contracts_result.keys())
                    logger.info(f"old {len(old_ids)}, new {len(new_ids)}")
                    new_links = new_ids - old_ids
                    if new_links:  # Announce newly detected hostile contracts.
                        link_list = "\n".join(
                            f"🔗 {sus_contracts_result[issuer_id]}" for issuer_id in new_links
                        )
                        logger.info(f"{corp_name} new assets:\n{link_list}")

                    if old_ids:  # Provide historical comparison for visibility.
                        old_link_list = "\n".join(
                            f"🔗 {old_contracts[issuer_id]}" for issuer_id in old_ids if issuer_id in old_contracts
                        )
                        logger.info(f"{corp_name} old assets:\n{old_link_list}")

                    if corpstatus.has_sus_contracts != has_sus_contracts:  # Flag boolean change in summary.
                        corp_changes.append(f"## Sus Contracts: {'🚩' if has_sus_contracts else '✖'}")
                    logger.info(f"{corp_name} status changed")

                    if new_links:  # Detail new contract notes per issuer.
                        corp_changes.append(f"## New Sus Contracts:")
                        for issuer_id in new_links:
                            res = sus_contracts_result[issuer_id]
                            ping = get_pings('New Sus Contracts')
                            if res.startswith("- A -"):  # Suppress pings for informational entries.
                                ping = ""
                            corp_changes.append(f"{res} {ping}")

                    corpstatus.has_sus_contracts = has_sus_contracts
                    corpstatus.sus_contracts = sus_contracts_result

                if corpstatus.has_sus_trans != has_sus_trans or set(sus_trans_result) != set(as_dict(corpstatus.sus_trans) or {}):  # Track transactional deltas as well.
                    old_trans = as_dict(corpstatus.sus_trans) or {}
                    #normalized_old = { str(cid): v for cid, v in status.sus_contacts.items() }
                    #normalized_new = { str(cid): v for cid, v in sus_contacts_result.items() }

                    old_ids   = set(as_dict(corpstatus.sus_trans).keys())
                    new_ids   = set(sus_trans_result.keys())
                    new_links = new_ids - old_ids
                    if new_links:  # Highlight new suspicious transactions.
                        link_list = "\n".join(
                            f"{sus_trans_result[issuer_id]}" for issuer_id in new_links
                        )
                        logger.info(f"{corp_name} new trans:\n{link_list}")

                    if old_ids:  # Keep log of previously known records for diff context.
                        old_link_list = "\n".join(
                            f"{old_trans[issuer_id]}" for issuer_id in old_ids if issuer_id in old_trans
                        )
                        logger.info(f"{corp_name} old trans:\n{old_link_list}")

                    if corpstatus.has_sus_trans != has_sus_trans:  # Change summary for top-level state.
                        corp_changes.append(f"## Sus Transactions: {'🚩' if has_sus_trans else '✖'}")
                    logger.info(f"{corp_name} status changed")
                    corp_changes.append(f"## New Sus Transactions{get_pings('New Sus Transactions')}:\n{link_list}")
                    #if new_links:
                    #    changes.append(f"## New Sus Transactions @here:")
                    #    for issuer_id in new_links:
                    #        res = sus_trans_result[issuer_id]
                    #        ping = f""
                    #        if res.startswith("- A -"):
                    #            ping = ""
                    #        changes.append(f"{res} {ping}")

                    corpstatus.has_sus_trans = has_sus_trans
                    corpstatus.sus_trans = sus_trans_result

                if corp_changes:  # Dispatch Discord updates only when changes were recorded.
                    for i in range(0, len(corp_changes)):
                        chunk = corp_changes[i]
                        if i == 0:  # First chunk gets the header to emphasize the corp name.
                            msg = f"# 🛑 Status change detected for **{corp_name}**:\n" + "\n" + chunk
                        else:
                            msg = chunk
                        logger.info(f"Measage: {msg}")
                        send_message(msg)
                        time.sleep(0.03)
                corpstatus.updated = timezone.now()
                corpstatus.save()

    except Exception as e:
        logger.error("Task failed", exc_info=True)
        instance.is_active = True
        instance.save()
        send_message(
            f"#{get_pings('Error')} Corp Brother encountered an unexpected error and disabled itself, "
            "please forward your aa worker.log and the error below to support"
        )

        tb_str = traceback.format_exc()
        max_chunk = 1000
        start = 0
        length = len(tb_str)

        while start < length:
            end = min(start + max_chunk, length)
            if end < length:  # Keep chunks within newline boundaries for readability.
                nl = tb_str.rfind('\n', start, end)
                if nl != -1 and nl > start:  # Prefer splitting on line breaks when possible.
                    end = nl + 1
            chunk = tb_str[start:end]
            send_message(f"```{chunk}```")
            start = end

    from django_celery_beat.models import PeriodicTask
    task_name = 'CB run regular updates'
    task = PeriodicTask.objects.filter(name=task_name).first()
    if not task.enabled:  # alert admins when the initial manual run has completed
        send_message("Corp Brother task has finished, you can now enable the task")


@shared_task
def check_member_compliance():
    """
    Nightly compliance sweep:
      • Ensures characters with corp roles still have valid corp tokens.
      • Reports missing characters per corp/alliance (via EveWho).
      • Sends a single consolidated Discord message with all findings.
    """
    instance = BigBrotherConfig.get_solo()
    if not instance.is_active:  # plugin disabled → skip expensive checks
        return
    users = get_users()
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
    corp_ids = instance.member_corporations
    if corp_ids:  # optionally check extra corp ids even if they’re outside auth
        for corp_id in corp_ids.split(","):
            corp_chars = []
            corp_id = corp_id.strip()
            if not corp_id:  # ignore blank entries
                continue

            # Get characters linked in your DB
            linked_chars = list(
                EveCharacter.objects.filter(corporation_id=corp_id)
                .values_list("character_name", flat=True)
            )

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
    ali_ids = instance.member_alliances
    logger.info(f"ali_ids: {str(ali_ids)}")
    if ali_ids:  # optional alliance-level audits
        for ali_id in ali_ids.split(","):
            logger.info(f"ali_id: {str(ali_id)}")
            ali_chars = []
            ali_id = ali_id.strip()
            logger.info(f"ali_id: {str(ali_id)}")
            if not ali_id:  # Ignore empty strings
                continue

            # Get characters linked in your DB
            linked_chars = list(
                EveCharacter.objects.filter(alliance_id=ali_id)
                .values_list("character_name", flat=True)
            )
            logger.info(f"linked_chars: {str(linked_chars)}")

            ali_name = get_alliance_name(ali_id)
            logger.info(f"ali_name: {str(ali_name)}")
            # Get characters from EveWho API
            all_ali_members = get_ali_character_names(ali_id)
            logger.info(f"all_ali_members: {str(all_ali_members)}")
            # Find missing characters
            for char_name in all_ali_members:
                if char_name not in linked_chars:  # missing from Auth → flag
                    ali_chars.append(f"  - {char_name}")
            if ali_chars:  # Only add block when missing alliance characters exist.
                chars_str = "\n".join(ali_chars)
                missing_characters.append(f"- {ali_name}\n{chars_str}")
    compliance_msg = ""
    if missing_characters:  # Prepend EveWho gaps when any exist.
        logger.info(f"missing_characters: {str(missing_characters)}")
        joined_msg = '\n'.join(missing_characters)
        compliance_msg += f"\n## Missing tokens for member characters:\n{joined_msg}"

    if messages:  # Attach per-user compliance flags when collected.
        compliance_msg += f"\n## Non Compliant users found:\n" + messages

    if compliance_msg:  # Only ping Discord when there is something to report.
        compliance_msg = f"#{get_pings('Compliance')} Compliance Issues found:" + compliance_msg
        send_message(compliance_msg)

import requests

def get_corp_character_names(corp_id: int) -> str:
    """Return the full list of member names for `corp_id` via EveWho."""
    url = f"https://evewho.com/api/corplist/{corp_id}"
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

    return [char["name"] for char in data.get("characters", [])]

def get_ali_character_names(ali_id: int) -> str:
    """Return the full list of member names for `ali_id` via EveWho."""
    url = f"https://evewho.com/api/allilist/{ali_id}"
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

    return [char["name"] for char in data.get("characters", [])]


@shared_task
def BB_send_daily_messages():
    """Send one random daily message to the configured webhook each run."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # DLC disabled → skip entirely
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task
def BB_send_opt_message1():
    """Send one optional message #1 if enabled and DLC active."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # Skip entirely when DLC disabled.
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task
def BB_send_opt_message2():
    """Optional message stream #2."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # DLC off → never send.
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task
def BB_send_opt_message3():
    """Optional message stream #3."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # Skip when DLC disabled.
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task
def BB_send_opt_message4():
    """Optional message stream #4."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # DLC off → skip.
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()

@shared_task
def BB_send_opt_message5():
    """Optional message stream #5."""
    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # DLC off → skip entire task.
        return
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
    send_message(message.text, webhook)

    # Mark as sent
    message.sent_in_cycle = True
    message.save()


@shared_task
def BB_register_message_tasks():
    """
    Ensure the celery-beat entries exist for the daily/optional message streams.
    Deletes tasks outright when the DLC is disabled.
    """
    logger.info("🔄 Running BB_register_message_tasks...")

    config = BigBrotherConfig.get_solo()
    if not config.dlc_daily_messages_active:  # remove scheduled tasks when DLC off
        PeriodicTask.objects.filter(
            name__in=[
                "BB send daily message",
                "BB send optional message 1",
                "BB send optional message 2",
                "BB send optional message 3",
                "BB send optional message 4",
                "BB send optional message 5",
            ]
        ).delete()
        logger.info("Daily message DLC disabled; skipping task registration.")
        return

    # Default fallback schedule (12:00 UTC daily)
    default_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute='0',
        hour='12',
        day_of_week='*',
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )

    # Tasks info: name, task path, config schedule attr, active flag attr
    tasks = [
        {
            "name": "BB send daily message",
            "task_path": "aa_bb.tasks_cb.BB_send_daily_messages",
            "schedule_attr": "dailyschedule",
            "active_attr": "are_daily_messages_active",
        },
        {
            "name": "BB send optional message 1",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message1",
            "schedule_attr": "optschedule1",
            "active_attr": "are_opt_messages1_active",
        },
        {
            "name": "BB send optional message 2",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message2",
            "schedule_attr": "optschedule2",
            "active_attr": "are_opt_messages2_active",
        },
        {
            "name": "BB send optional message 3",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message3",
            "schedule_attr": "optschedule3",
            "active_attr": "are_opt_messages3_active",
        },
        {
            "name": "BB send optional message 4",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message4",
            "schedule_attr": "optschedule4",
            "active_attr": "are_opt_messages4_active",
        },
        {
            "name": "BB send optional message 5",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message5",
            "schedule_attr": "optschedule5",
            "active_attr": "are_opt_messages5_active",
        },
    ]

    for task_info in tasks:
        name = task_info["name"]
        task_path = task_info["task_path"]
        schedule = getattr(config, task_info["schedule_attr"], None) or default_schedule
        is_active = getattr(config, task_info["active_attr"], False)

        existing_task = PeriodicTask.objects.filter(name=name).first()

        if is_active:  # ensure the periodic task exists/enabled when feed is on
            if existing_task is None:  # Nothing scheduled yet; create it.
                PeriodicTask.objects.create(
                    name=name,
                    task=task_path,
                    crontab=schedule,
                    enabled=True,
                )
                logger.info(f"✅ Created '{name}' periodic task with enabled=True")
            else:
                updated = False
                if existing_task.crontab != schedule:  # Update schedule if admin changed it.
                    existing_task.crontab = schedule
                    updated = True
                if existing_task.task != task_path:  # Ensure callable matches configuration.
                    existing_task.task = task_path
                    updated = True
                if not existing_task.enabled:  # Re-enable tasks that were left disabled.
                    existing_task.enabled = True
                    updated = True
                if updated:  # Persist/log only when the model was mutated.
                    existing_task.save()
                    logger.info(f"✅ Updated '{name}' periodic task")
                else:
                    logger.info(f"ℹ️ '{name}' periodic task already exists and is up to date")
        else:  # feed disabled → delete scheduled task
            if existing_task:  # Remove the stale beat entry to avoid stray posts.
                existing_task.delete()
                logger.info(f"🗑️ Deleted '{name}' periodic task because messages are disabled")



@shared_task
def BB_run_regular_loa_updates():
    """
    Scan every member main and update LoA statuses / inactivity flags.

    - Skips entirely when the LoA DLC or feature toggle is off.
    - Marks approved requests as in-progress/finished based on dates.
    - Sends LoA inactivity warnings when a pilot exceeds the allowed logoff days
      without an LoA in progress.
    """
    cfg = BigBrotherConfig.get_solo()
    if not cfg.dlc_loa_active:  # Bail out when LoA DLC is not purchased.
        logger.info("LoA DLC disabled; skipping BB_run_regular_loa_updates.")
        return
    if not cfg.is_loa_active:  # Admin turned off the LoA feature.
        logger.info("LoA feature disabled; skipping updates.")
        return
    member_states = BigBrotherConfig.get_solo().bb_member_states.all()
    qs_profiles = (
        UserProfile.objects
        .filter(state__in=member_states)
        .exclude(main_character=None)
        .select_related("user", "main_character")
    )
    if not qs_profiles.exists():  # No members matching filters, so nothing to process.
        logger.info("No member mains found.")
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
                send_message(f"{user.username}'s LoA Request status changed to in progress")
            elif today > lr.end_date and lr.status != "finished":  # Auto-close requests whose end dates passed.
                lr.status = "finished"
                lr.save(update_fields=["status"])
                send_message(f"##{get_pings('LoA Changed Status')} **{ec}**'s LoA\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- for **{lr.reason}**\n## has finished")
        has_active_loa = LeaveRequest.objects.filter(
            user=user,
            status="in_progress",
            start_date__lte=today,
            end_date__gte=today,
        ).exists()
        if days_since > cfg.loa_max_logoff_days and not has_active_loa:  # Flag members inactive beyond policy without LoA.
            flags.append(f"- **{ec}** was last seen online on {latest_logoff} (**{days_since}** days ago where maximum w/o a LoA request is **{cfg.loa_max_logoff_days}**)")
    if flags:  # Notify staff when inactivity breaches are detected.
        flags_text = "\n".join(flags)
        send_message(f"##{get_pings('LoA Inactivity')} Inactive Members Found:\n{flags_text}")


@shared_task
def BB_daily_DB_cleanup():
    """
    Periodic cleanup of cached tables and orphaned processed records.

    Deletes stale name caches, employment caches, processed mail/contract/transaction
    entries that no longer have backing data, and non-member PAP compliance rows.
    """
    from .models import (
        Alliance_names, Character_names, Corporation_names, UserStatus, EntityInfoCache,
        id_types,
    )
    from .modelss import (
        CharacterEmploymentCache, FrequentCorpChangesCache, CurrentStintCache, AwoxKillsCache,
        CorporationInfoCache, AllianceHistoryCache, SovereigntyMapCache,
    )
    two_months_ago = timezone.now() - timedelta(days=60)
    flags = []
    #Delete old model entries
    models_to_cleanup = [
        (Alliance_names, "alliance"),
        (Character_names, "character"),
        (Corporation_names, "corporation"),
        (UserStatus, "User Status"),
        (EntityInfoCache, "Entity Info Cache"),
        (CorporationInfoCache, "Corporation Info Cache"),
        (AllianceHistoryCache, "Alliance History Cache"),
        (SovereigntyMapCache, "Sovereignty Map Cache"),
    ]

    for model, name in models_to_cleanup:
        old_entries = model.objects.filter(updated__lt=two_months_ago)
        count, _ = old_entries.delete()
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
            old_entries = model.objects.filter(last_accessed__lt=two_months_ago)
            count, _ = old_entries.delete()
            flags.append(f"- Deleted {count} old {name} records (by last access).")
        except Exception:
            continue

    # id_types: delete if not looked up in last 60 days
    try:
        stale_ids = id_types.objects.filter(last_accessed__lt=two_months_ago)
        count, _ = stale_ids.delete()
        flags.append(f"- Deleted {count} old ID type cache records (by last access).")
    except Exception:
        pass

    # -- CONTRACTS --
    # Get all contract_ids that exist in Contract
    existing_CorporateContract_ids = set(
        CorporateContract.objects.values_list('contract_id', flat=True)
    )
    existing_playercontract_ids = set(
        Contract.objects.values_list('contract_id', flat=True)
    )
    existing_contract_ids = existing_CorporateContract_ids | existing_playercontract_ids

    # Find ProcessedContract entries not in Contract
    orphaned_processed_contracts = ProcessedContract.objects.exclude(contract_id__in=existing_contract_ids)
    orphaned_contract_ids = list(orphaned_processed_contracts.values_list('contract_id', flat=True))

    # Delete orphans in SusContractNote (OneToOneField links to ProcessedContract)
    sus_contracts_to_delete = SusContractNote.objects.filter(contract_id__in=orphaned_contract_ids)

    with transaction.atomic():
        count_sus = sus_contracts_to_delete.delete()[0]
        count_proc = orphaned_processed_contracts.delete()[0]

    flags.append(f"- Deleted {count_proc} old ProcessedContract and {count_sus} SusContractNote records.")

    # -- MAILS --
    existing_mail_ids = set(
        MailMessage.objects.values_list('id_key', flat=True)
    )

    orphaned_processed_mails = ProcessedMail.objects.exclude(mail_id__in=existing_mail_ids)
    orphaned_mail_ids = list(orphaned_processed_mails.values_list('mail_id', flat=True))

    sus_mails_to_delete = SusMailNote.objects.filter(mail_id__in=orphaned_mail_ids)

    with transaction.atomic():
        count_sus = sus_mails_to_delete.delete()[0]
        count_proc = orphaned_processed_mails.delete()[0]

    flags.append(f"- Deleted {count_proc} old ProcessedMail and {count_sus} SusMailNote records.")

    # -- TRANSACTIONS --
    existing_entry_ids = (
        set(CharacterWalletJournalEntry.objects.values_list('entry_id', flat=True))
        | set(CorporationWalletJournalEntry.objects.values_list('entry_id', flat=True))
    )

    orphaned_processed_transactions = ProcessedTransaction.objects.exclude(entry_id__in=existing_entry_ids)
    orphaned_entry_ids = list(orphaned_processed_transactions.values_list('entry_id', flat=True))

    sus_transactions_to_delete = SusTransactionNote.objects.filter(transaction_id__in=orphaned_entry_ids)

    with transaction.atomic():
        count_sus = sus_transactions_to_delete.delete()[0]
        count_proc = orphaned_processed_transactions.delete()[0]

    flags.append(f"- Deleted {count_proc} old ProcessedTransaction and {count_sus} SusTransactionNote records.")

    # -- PAP COMPLIANCE: drop entries for non-members --
    try:
        member_profile_ids = list(get_user_profiles().values_list('id', flat=True))
        non_member_pc_qs = PapCompliance.objects.exclude(user_profile_id__in=member_profile_ids)
        deleted_pc = non_member_pc_qs.delete()[0]
        flags.append(f"- Deleted {deleted_pc} PapCompliance records for non-members.")
    except Exception as e:
        logger.warning(f"PapCompliance cleanup failed: {e}")

    if flags:  # Summarize cleanup actions when anything was removed.
        flags_text = "\n".join(flags)
        send_message(f"### DB Cleanup Complete:\n{flags_text}")
