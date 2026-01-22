"""Celery tasks and helpers that manage compliance tickets and reminders."""

import time
from typing import Optional

from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import close_old_connections

from celery import shared_task

from allianceauth.authentication.models import UserProfile
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)

from .app_settings import get_user_profiles, get_character_id, send_status_embed, _chunk_embed_lines, send_message, afat_active, discordbot_active, corptools_active

try:
    if discordbot_active():
        from aadiscordbot.tasks import run_task_function
        from aadiscordbot.utils.auth import get_discord_user_id
        from aadiscordbot.cogs.utils.exceptions import NotAuthenticated
        from aadiscordbot.app_settings import get_admins
    else:
        raise ImportError("aadiscordbot not installed")
except ImportError:
    run_task_function = None
    get_discord_user_id = None

    class NotAuthenticated(Exception):
        pass
    get_admins = None

try:
    if corptools_active():
        from corptools.api.helpers import get_alts_queryset
    else:
        get_alts_queryset = None
except ImportError:
    get_alts_queryset = None

if get_alts_queryset is None:
    def get_alts_queryset(*args, **kwargs):
        return []

from .models import BigBrotherConfig, TicketToolConfig, PapCompliance, LeaveRequest, ComplianceTicket, ComplianceTicketComment

User = get_user_model()


def corp_check(user) -> bool:
    """
    Determine whether the user passes the corp compliance filter.

    Returns True whenever the corp check feature is disabled or when the current
    ComplianceFilter evaluates truthy for the account.
    """
    if not TicketToolConfig.get_solo().corp_check_enabled:  # Feature disabled -> automatically compliant.
        return True
    try:
        cfg: Optional[TicketToolConfig] = TicketToolConfig.get_solo()
    except Exception:
        # If the singleton isn't set up yet, be lenient.
        logger.warning("✅  [AA-BB] - [corp_check] - TicketToolConfig.get_solo() failed; treating user as compliant.")
        return True

    # Check if compliance_filter field exists (charlink installed)
    if not hasattr(cfg, 'compliance_filter'):
        logger.warning("✅  [AA-BB] - [corp_check] - charlink not installed, compliance_filter unavailable; treating user as compliant.")
        return True

    if not cfg or not cfg.compliance_filter:  # Missing configuration leaves everyone compliant.
        return True

    try:
        # process_filter(user) returns the 'check' boolean for this user,
        # where 'check' already applies the filter and the 'negate' flag.
        return bool(cfg.compliance_filter.process_filter(user))
    except Exception:
        # Misconfiguration or unexpected error: log and be lenient.
        logger.exception("✅  [AA-BB] - [corp_check] - Error while running compliance filter for user id=%s", user.id)
        return True


def paps_check(user):
    """
    Inspect PAP compliance state for the user, honoring LoA in-progress status.

    Returns True when PAP checks are disabled, the user has an LoA pending, or
    their PapCompliance row indicates compliance.
    """
    if not afat_active() or not TicketToolConfig.get_solo().paps_check_enabled:  # Globally disabled -> compliant.
        return True
    lr_qs = LeaveRequest.objects.filter(
            user=user,
            status="in_progress",
        ).exists()
    if lr_qs:  # Active LoA suppresses PAP enforcement.
        return True
    try:
        profile = user.profile  # thanks to related_name='profile'
    except UserProfile.DoesNotExist:
        return True  # No profile at all, treat as compliant

    pc = PapCompliance.objects.filter(user_profile=profile).first()
    if not pc:  # Without compliance data the check cannot fail the user.
        return True

    return pc.pap_compliant > 0


def afk_check(user):
    """
    Evaluate AFK compliance based on most recent logoff among the user's alts.

    Returns False if no logoff data exists, the main is missing, or the latest
    logout exceeds the configured max AFK days.
    """
    if not TicketToolConfig.get_solo().afk_check_enabled:  # Disabled toggle => user passes check.
        return True
    tcfg = TicketToolConfig.get_solo()
    max_afk_days = tcfg.Max_Afk_Days
    lr_qs = LeaveRequest.objects.filter(
            user=user,
            status="in_progress",
        ).exists()
    if lr_qs:  # LoA overrides AFK failures.
        return True
    profile = UserProfile.objects.get(user=user)
    if not profile:  # Missing profile prevents further evaluation.
        return False
    try:
        main_id = profile.main_character.character_id
    except Exception:
        main_id = get_character_id(profile)

    # Load main character
    ec = EveCharacter.objects.filter(character_id=main_id).first()
    if not ec:  # Cannot determine AFK if the main character record is missing.
        return False

    # Find the most recent logoff among all alts (cached to avoid repeated queries)
    latest_logoff = _get_latest_logoff_cached(ec.character_id)

    if not latest_logoff:  # No logoff information means fail the AFK check.
        return False

    # Compute days since that logoff
    days_since = (timezone.now() - latest_logoff).days
    if days_since >= max_afk_days:  # Too many days inactive triggers failure.
        return False
    return True


def _get_latest_logoff_cached(char_id):
    """Cache latest logoff time for character to avoid repeated alt queries."""
    from django.core.cache import cache
    cache_key = f"aa_bb_latest_logoff_{char_id}"
    cached = cache.get(cache_key)

    if cached is not None:
        return cached

    ec = EveCharacter.objects.filter(character_id=char_id).first()
    if not ec:
        # Cache negative result to prevent repeated DB queries
        cache.set(cache_key, None, 600)  # 10 min for missing chars
        return None

    latest_logoff = None
    alts = get_alts_queryset(ec)
    # Use select_related to reduce queries
    if hasattr(alts, 'select_related'):
        alts = alts.select_related('characteraudit')

    for char in alts:
        audit = getattr(char, "characteraudit", None)
        ts = getattr(audit, "last_known_logoff", None) if audit else None
        if ts and (latest_logoff is None or ts > latest_logoff):
            latest_logoff = ts

    # Cache for 1 hour
    cache.set(cache_key, latest_logoff, 3600)
    return latest_logoff


def discord_check(user):
    """
    Ensure the user has authenticated a Discord account if the feature is enabled.
    """
    if not discordbot_active() or not TicketToolConfig.get_solo().discord_check_enabled:  # Disabled toggle permits everyone.
        return True
    try:
        discord_id = get_discord_user_id(user)
    except NotAuthenticated:
        return False  # Missing Discord auth fails the check.
    return True


def discord_inactivity_check(user):
    """
    Check if the user has spoken on Discord within the configured timeframe.
    """
    tcfg = TicketToolConfig.get_solo()
    bbcfg = BigBrotherConfig.get_solo()
    if not discordbot_active() or not tcfg.discord_inactivity_enabled or not bbcfg.discord_message_tracking:
        return True

    from .models import UserStatus
    status_data = UserStatus.objects.filter(user=user).values("last_discord_message_at").first()

    if not status_data:
        # If no status recorded, initialize one to avoid mass ticketing on feature activation.
        try:
            get_discord_user_id(user)
            UserStatus.objects.create(user=user, last_discord_message_at=timezone.now())
        except Exception:
            pass
        return True

    last_discord_message_at = status_data.get("last_discord_message_at")
    if not last_discord_message_at:
        try:
            get_discord_user_id(user)
            UserStatus.objects.filter(user=user).update(last_discord_message_at=timezone.now())
        except Exception:
            pass
        return True

    days_since = (timezone.now() - last_discord_message_at).days
    if days_since >= tcfg.discord_inactivity_days:
        return False
    return True



def get_webhook_for_reason(reason: str) -> Optional[str]:
    """Resolve which webhook URL to use based on the ticket reason."""
    bb_cfg = BigBrotherConfig.get_solo()
    if reason in ["paps_check", "afk_check", "discord_check", "char_removed"]:
        return bb_cfg.user_compliance_webhook or bb_cfg.webhook
    if reason in ["corp_check", "awox_kill"]:
        return bb_cfg.corp_compliance_webhook or bb_cfg.webhook
    return bb_cfg.webhook


@shared_task(time_limit=7200)
def hourly_compliance_check():
    """Run the top-of-hour audit that enforces compliance rules and reminders."""
    close_old_connections()
    bb_cfg = BigBrotherConfig.get_solo()
    if not bb_cfg.is_active:
        logger.warning("ℹ️  [AA-BB] - [hourly_compliance_check] - Plugin is disabled (is_active=False), skipping compliance check.")
        return
    t_cfg = TicketToolConfig.get_solo()
    max_days = {
        "corp_check": t_cfg.corp_check,
        "paps_check": t_cfg.paps_check,
        "afk_check": t_cfg.afk_check,
        "discord_check": t_cfg.discord_check,
        "discord_inactivity": t_cfg.discord_inactivity_days,
    }

    # Per-reason reminder frequency (in days)
    reminder_frequency = {
        "corp_check": t_cfg.corp_check_frequency,
        "paps_check": t_cfg.paps_check_frequency,
        "afk_check": t_cfg.afk_check_frequency,
        "discord_check": t_cfg.discord_check_frequency,
        "discord_inactivity": 1, # Default to 1 day
    }

    reason_checkers = {
        "corp_check": (corp_check, t_cfg.corp_check_reason),
        "afk_check": (afk_check, t_cfg.afk_check_reason),
        "discord_check": (discord_check, t_cfg.discord_check_reason),
        "discord_inactivity": (discord_inactivity_check, t_cfg.discord_inactivity_reason),
    }

    reminder_messages = {
        "corp_check": t_cfg.corp_check_reminder,
        "afk_check": t_cfg.afk_check_reminder,
        "discord_check": t_cfg.discord_check_reminder,
        "discord_inactivity": t_cfg.discord_inactivity_reason, # Use reason msg as reminder too if no dedicated field
    }

    if afat_active():
        reason_checkers["paps_check"] = (paps_check, t_cfg.paps_check_reason)
        reminder_messages["paps_check"] = t_cfg.paps_check_reminder

    now = timezone.now()

    profiles_qs = get_user_profiles()
    if bb_cfg.limit_to_main_corp:
        profiles_qs = profiles_qs.filter(main_character__corporation_id=bb_cfg.main_corporation_id)

    # Bulk load profile IDs to define allowed membership scope efficiently
    allowed_user_ids = set(profiles_qs.values_list('user_id', flat=True))

    # Pre-fetch excluded users to avoid repeated queries
    excluded_user_ids = set(t_cfg.excluded_users.all().values_list('id', flat=True))

    # 1. Check compliance reasons
    for profile in profiles_qs.select_related('user', 'main_character').iterator():
        user = profile.user
        if user.id in excluded_user_ids:  # Skip users explicitly excluded from checks.
            continue
        for reason, (checker, msg_template) in reason_checkers.items():
            checked = checker(user)
            if not checked:  # Non-compliant result requires a ticket/ensuring existing one.
                logger.info(f"✅  [AA-BB] - [hourly_compliance_check] - user{user},reason{reason},checked{checked}")
                ensure_ticket(user, reason)

    # 2. Process existing tickets
    ticket_resolved_manually_notify = bb_cfg.ticket_notify_man
    ticket_resolved_automatic_notify = bb_cfg.ticket_notify_auto

    # For grouping notifications by webhook
    notifications_by_hook: dict[str, list[str]] = {}

    def add_notification(hook_url, msg):
        if hook_url not in notifications_by_hook:
            notifications_by_hook[hook_url] = []
        notifications_by_hook[hook_url].append(msg)

    tickets_qs = ComplianceTicket.objects.filter(is_resolved=False, is_exception=False)
    if bb_cfg.limit_to_main_corp:
        tickets_qs = tickets_qs.filter(user__profile__main_character__corporation_id=bb_cfg.main_corporation_id)

    for ticket in tickets_qs:
        reason = ticket.reason
        hook = get_webhook_for_reason(reason)

        # Build a display name for the user (mention or plain text)
        if ticket.user:
            user_display = f"**{ticket.user.username}**" if (ticket.discord_user_id == 0 or reason == "discord_check") else f"<@{ticket.discord_user_id}>"
        else:
            user_display = f"<@{ticket.discord_user_id}>" if ticket.discord_user_id else "Unknown User"

        # Skip exception tickets
        if ticket.is_exception:
            continue

        if reason == "awox_kill":
            # These rely on manual resolution flow and currently have no automated reminders
            continue

        if reason == "char_removed":
            if not ticket.user:
                close_ticket(ticket)
                if ticket_resolved_automatic_notify:
                    add_notification(hook, f"⚠️ Ticket for {user_display} (**{reason}**) closed due to missing auth user")
                continue

            if not ticket.details:
                # No character name stored, can't auto-resolve
                continue

            # Check if characters are re-added
            char_names = [c.strip() for c in ticket.details.split(",") if c.strip()]
            if not char_names:
                continue

            all_readded = True
            for char_name in char_names:
                if not EveCharacter.objects.filter(character_name=char_name, character_ownership__user=ticket.user).exists():
                    all_readded = False
                    break

            if all_readded:
                close_ticket(ticket)
                if ticket_resolved_automatic_notify:
                    add_notification(hook, f"✅ Ticket for {user_display} (**{reason}**) resolved (all characters re-added: {ticket.details})")
                continue
            else:
                # Still missing some characters, continue to wait
                continue

        checker, _ = reason_checkers[reason]

        # resolved?
        if ticket.user and checker(ticket.user):  # Condition cleared, close and notify.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"✅ Ticket for {user_display} (**{reason}**) resolved")
            continue

        if ticket.user_id not in allowed_user_ids:  # User left the org, close ticket and alert.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"❌ User {user_display} is no longer a member, closing ticket (**{reason}**)")
            continue

        if not ticket.user:  # Missing auth user entirely, close ticket.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"⚠️ Ticket for {user_display} (**{reason}**) closed due to missing auth user")
            continue

        # Reminder logic with per-reason frequency + max-days cap
        days_elapsed = (now - ticket.created_at).days
        if days_elapsed <= 0:  # Do not send reminders on the same day ticket was created.
            continue  # don't ping on creation day

        # Check frequency BEFORE sending reminders
        freq_days = reminder_frequency.get(reason, 1)
        last_day_pinged = ticket.last_reminder_sent or 0
        if (days_elapsed - last_day_pinged) < freq_days:
            continue

        max_dayss = max_days.get(reason, 30)

        # Build the normal reminder message: mention the user + role + days left
        days_left = max(0, max_dayss - days_elapsed)
        template = reminder_messages[reason]  # must support {namee}, {role}, {days}

        # Check include_user flag for this reason
        tcfg = TicketToolConfig.get_solo()
        include_user_flags = {
            "corp_check": tcfg.corp_check_include_user,
            "afk_check": tcfg.afk_check_include_user,
            "discord_check": tcfg.discord_check_include_user,
            "paps_check": getattr(tcfg, "paps_check_include_user", True) if afat_active() else True,
            "discord_inactivity": tcfg.discord_inactivity_include_user,
        }
        include_user = include_user_flags.get(reason, True)

        if reason == "discord_check" or ticket.discord_user_id == 0 or not include_user:
            mention = f"{ticket.user.username}"
            template = template.replace("<@{namee}>", "**{namee}**")
        else:
            mention = f"{ticket.discord_user_id}"

        if reason == "paps_check":  # PAP reminder template only uses {days}.
            msg = template.format(days=days_left)
        else:
            role_ping = ""
            if t_cfg.role_id:
                ids = [i.strip() for i in str(t_cfg.role_id).split(",") if i.strip()]
                role_ping = "><@&".join(ids)
            msg = template.format(namee=mention, role=role_ping, days=days_left)
            if not role_ping:
                msg = msg.replace("<@&>,", "").replace("<@&>", "")

        # Queue the bot-side reminder (ensure task_kwargs is present)
        if run_task_function:
            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.send_ticket_reminder"],
                kwargs={
                    "task_args": [ticket.discord_channel_id, ticket.discord_user_id, msg],
                    "task_kwargs": {}
                },
                queue='aadiscordbot'
            )

        # Mark today as reminded so the system does not ping again today
        ticket.last_reminder_sent = days_elapsed
        ticket.save(update_fields=["last_reminder_sent"])

    # Flush grouped notifications
    for hook_url, lines in notifications_by_hook.items():
        chunks = _chunk_embed_lines(lines)
        for chunk in chunks:
            send_status_embed(
                subject="Ticket Updates",
                lines=chunk,
                color=0x3498db,  # Blue
                hook=hook_url
            )

    # Rebalance ticket categories after processing tickets
    try:
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.rebalance_ticket_categories"],
            kwargs={
                "task_args": [],
                "task_kwargs": {}
            },
            queue='aadiscordbot'
        )
    except Exception:
        # Non-fatal if scheduling fails
        pass


def ensure_ticket(user, reason, details=None):
    """
    Guarantee there is an open compliance ticket for the given user/reason pair.

    Handles Discord lookup, fallbacks, and message templating before delegating
    the actual ticket creation to the bot worker.
    """
    tcfg = TicketToolConfig.get_solo()
    role_ping = "><@&".join([i.strip() for i in str(tcfg.role_id).split(",") if i.strip()]) if tcfg.role_id else ""
    max_afk_days = tcfg.Max_Afk_Days
    reason_checkers = {
        "corp_check": (corp_check, tcfg.corp_check_reason),
        "afk_check": (afk_check, tcfg.afk_check_reason),
        "discord_check": (discord_check, tcfg.discord_check_reason),
        "char_removed": (None, tcfg.char_removed_reason),
        "awox_kill": (None, tcfg.awox_kill_reason),
        "discord_inactivity": (discord_inactivity_check, tcfg.discord_inactivity_reason),
    }
    if afat_active():
        reason_checkers["paps_check"] = (paps_check, tcfg.paps_check_reason)

    if reason == "paps_check" and not afat_active():
        logger.warning(f"Attempted to create PAPs ticket but afat is not active.")
        return

    include_user_flags = {
        "corp_check": tcfg.corp_check_include_user,
        "afk_check": tcfg.afk_check_include_user,
        "discord_check": tcfg.discord_check_include_user,
        "char_removed": tcfg.char_removed_include_user,
        "awox_kill": tcfg.awox_kill_include_user,
        "paps_check": getattr(tcfg, "paps_check_include_user", True) if afat_active() else True,
        "discord_inactivity": tcfg.discord_inactivity_include_user,
    }
    include_user = include_user_flags.get(reason, True)

    try:
        if get_discord_user_id:
            discord_id = get_discord_user_id(user)
        else:
            raise NotAuthenticated("aadiscordbot not installed")

        username = ""
        _, msg_template = reason_checkers[reason]

        # For discord_check reason, we never want to mention the user (as they aren't on discord)
        # and we don't want to fallback to superuser.
        if reason == "discord_check":
            include_user = False
            msg_template = msg_template.replace("<@{namee}>", "**{namee}**")

        if not include_user:
            msg_template = msg_template.replace("<@{namee}>", "**{namee}**")
            discord_id = 0

        namee_val = discord_id if include_user else user.username

        if reason == "afk_check":  # AFK templates expect {days}.
            ticket_message = msg_template.format(namee=namee_val, role=role_ping, days=max_afk_days)
        elif reason == "discord_inactivity":
            ticket_message = msg_template.format(namee=namee_val, role=role_ping, days=tcfg.discord_inactivity_days)
        elif reason == "discord_check":  # Discord-specific template uses username, not Discord mention.
            username = user.username
            ticket_message = msg_template.format(namee=username, role=role_ping, days=max_afk_days)
        elif reason in ["char_removed", "awox_kill"]:
            ticket_message = msg_template.format(namee=namee_val, role=role_ping, details=details)
        else:
            ticket_message = msg_template.format(namee=namee_val, role=role_ping)

        if not role_ping:
            ticket_message = ticket_message.replace("<@&>,", "").replace("<@&>", "")
    except NotAuthenticated:
        # User has no Discord - use text username instead of pinging
        username = user.username
        discord_id = 0

        _, msg_template = reason_checkers[reason]

        # Use plain text name in the template (no Discord mention)
        msg_template = msg_template.replace("<@{namee}>", "**{namee}**")

        if reason == "afk_check":
            ticket_message = msg_template.format(namee=user.username, role=role_ping, days=max_afk_days)
        elif reason == "discord_inactivity":
            ticket_message = msg_template.format(namee=user.username, role=role_ping, days=tcfg.discord_inactivity_days)
        elif reason == "discord_check":
            ticket_message = msg_template.format(namee=user.username, role=role_ping, days=max_afk_days)
        elif reason in ["char_removed", "awox_kill"]:
            ticket_message = msg_template.format(namee=user.username, role=role_ping, details=details)
        else:
            ticket_message = msg_template.format(namee=user.username, role=role_ping)

        if not role_ping:
            ticket_message = ticket_message.replace("<@&>,", "").replace("<@&>", "")

    # prevent duplicates and check for exceptions
    existing = ComplianceTicket.objects.filter(
        user=user, reason=reason
    ).first()

    if existing:
        if not existing.is_resolved:
            if reason == "char_removed" and details and details not in (existing.details or ""):
                if existing.details:
                    existing.details += f", {details}"
                else:
                    existing.details = details
                existing.save(update_fields=["details"])
                add_ticket_comment(existing, None, f"⚠️ Additional character removed: {details}")
            return  # Already has an open ticket

        # Reopen existing resolved ticket instead of creating a new one
        if reason == "char_removed" and details:
            existing.details = details
            existing.save(update_fields=["details"])
        reopen_ticket(existing, message=f"⚠️ Issue re-detected:\n{ticket_message}")

        send_status_embed(
            subject="Ticket Reopened",
            lines=[f"Ticket for **{user.username}** reopened, reason - **{reason}**"],
            color=0xf1c40f,  # Yellow
            hook=get_webhook_for_reason(reason)
        )
        return

    # If an exception exists for this user/reason, don't create a new ticket
    exception_exists = ComplianceTicket.objects.filter(
        user=user, reason=reason, is_exception=True
    ).exists()

    if exception_exists:
        logger.info(f"Skipping ticket creation for {user.username} ({reason}) - exception exists")
        return

    if not existing:  # Only emit side effects when a new ticket is needed.
        # Add 2 second delay to avoid Discord API rate limiting when creating multiple tickets
        time.sleep(2)

        send_status_embed(
            subject="Ticket Created",
            lines=[f"Ticket for **{user.username}** created, reason - **{reason}**"],
            color=0xf1c40f,  # Yellow
            hook=get_webhook_for_reason(reason)
        )

        ticket_type = tcfg.ticket_type

        # Fallback logic if bot is missing
        if not run_task_function and ticket_type in [TicketToolConfig.TICKET_TYPE_PRIVATE_CHANNEL, TicketToolConfig.TICKET_TYPE_PRIVATE_THREAD]:
            if tcfg.hr_forum_webhook:
                ticket_type = TicketToolConfig.TICKET_TYPE_FORUM_THREAD
            else:
                ticket_type = TicketToolConfig.TICKET_TYPE_AUTH_ONLY

        if ticket_type == TicketToolConfig.TICKET_TYPE_AUTH_ONLY:
            # Create local record only
            ComplianceTicket.objects.create(
                user=user,
                discord_user_id=0,
                discord_channel_id=None,
                reason=reason,
                ticket_id=tcfg.ticket_counter,
                details=details
            )
            tcfg.ticket_counter += 1
            tcfg.save(update_fields=["ticket_counter"])
            return

        from .models import ComplianceThread
        thread_obj = ComplianceThread.objects.filter(user=user, reason=reason).first()
        thread_id = thread_obj.thread_id if thread_obj else None

        main_char = getattr(user.profile, "main_character", None)
        char_name = main_char.character_name if main_char else user.username
        char_id = main_char.character_id if main_char else 0

        # Build thread name: Main Character - Reason (prettified)
        reason_display = dict(ComplianceTicket.REASONS).get(reason, reason)
        thread_name = f"{char_name} - {reason_display}"

        content = f"{ticket_message}\n\nAA Link: {settings.SITE_URL}/audit/r/{char_id}/account/status"

        if ticket_type == TicketToolConfig.TICKET_TYPE_FORUM_THREAD:
            # Forum thread via webhook
            if thread_id:
                # Reuse existing thread
                webhook_url = f"{tcfg.hr_forum_webhook}?thread_id={thread_id}"
                send_message({"content": content}, hook=webhook_url)
            else:
                # Create new thread
                thread_body = {
                    "thread_name": thread_name,
                    "content": content,
                }
                response = send_message(thread_body, hook=f"{tcfg.hr_forum_webhook}?wait=true")
                if response:
                    try:
                        data = response.json()
                        thread_id = data.get('id') or data.get('channel_id')
                        if thread_id:
                            ComplianceThread.objects.update_or_create(
                                user=user, reason=reason,
                                defaults={'thread_id': thread_id}
                            )
                    except Exception:
                        logger.exception("Failed to parse thread ID from webhook")

            # Create local record
            ComplianceTicket.objects.create(
                user=user,
                discord_user_id=discord_id,
                discord_channel_id=thread_id,
                reason=reason,
                ticket_id=tcfg.ticket_counter,
                details=details
            )
            tcfg.ticket_counter += 1
            tcfg.save(update_fields=["ticket_counter"])

            if run_task_function and thread_id:
                run_task_function.apply_async(
                    args=["aa_bb.tasks_bot.unarchive_thread"],
                    kwargs={
                        "task_args": [thread_id],
                        "task_kwargs": {}
                    },
                    queue='aadiscordbot'
                )

        elif ticket_type == TicketToolConfig.TICKET_TYPE_PRIVATE_THREAD:
            # Private thread via bot
            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.create_compliance_thread"],
                kwargs={
                    "task_args": [user.id, discord_id, reason, ticket_message, thread_name],
                    "task_kwargs": {"thread_id": thread_id, "include_user": include_user, "details": details}
                },
                queue='aadiscordbot'
            )

        elif ticket_type == TicketToolConfig.TICKET_TYPE_PRIVATE_CHANNEL:
            # Legacy Private Channel
            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.create_compliance_ticket"],
                kwargs={
                    "task_args": [user.id, discord_id, reason, ticket_message],
                    "task_kwargs": {"include_user": include_user, "details": details}
                },
                queue='aadiscordbot'
            )


def add_ticket_comment(ticket, user, comment_text, post_to_discord=True):
    """Add a comment to a ticket and optionally post to Discord."""
    from .models import ComplianceTicketComment, TicketToolConfig
    comment = ComplianceTicketComment.objects.create(
        ticket=ticket,
        user=user,
        comment=comment_text
    )
    if post_to_discord and ticket.discord_channel_id:
        tcfg = TicketToolConfig.get_solo()
        attribution = ""
        if user:
            main_char = getattr(user.profile, "main_character", None)
            attribution = f"**{main_char.character_name if main_char else user.username}**: "
        full_content = f"{attribution}{comment_text}"

        if tcfg.ticket_type == TicketToolConfig.TICKET_TYPE_FORUM_THREAD and tcfg.hr_forum_webhook:
            webhook_url = f"{tcfg.hr_forum_webhook}?thread_id={ticket.discord_channel_id}"
            embed = {
                "title": "Ticket Comment",
                "description": full_content,
                "color": 0xFFA500  # Orange
            }
            send_message({"embeds": [embed]}, hook=webhook_url)
        elif run_task_function:
            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.send_ticket_reminder"],
                kwargs={
                    "task_args": [ticket.discord_channel_id, 0, full_content],
                    "task_kwargs": {}
                },
                queue='aadiscordbot'
            )
    return comment


def close_ticket(ticket, user=None):
    """Close the Discord compliance ticket and mark it resolved locally."""
    message = "✅ Ticket resolved"
    if user:
        main_char = getattr(user.profile, "main_character", None)
        name = main_char.character_name if main_char else user.username
        message += f" by **{name}**"

    # Create Auth comment
    add_ticket_comment(ticket, user, message, post_to_discord=False)

    if run_task_function and ticket.discord_channel_id:
        # Bot manages the channel, send message via close task to ensure it's seen before closing
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.close_ticket_channel"],
            kwargs={
                "task_args": [ticket.discord_channel_id],
                "task_kwargs": {"message": message}
            },
            queue='aadiscordbot'
        )
    elif ticket.discord_channel_id:
        # Not bot managed (e.g. forum thread via webhook), send message normally
        from .models import TicketToolConfig
        tcfg = TicketToolConfig.get_solo()
        if tcfg.ticket_type == TicketToolConfig.TICKET_TYPE_FORUM_THREAD and tcfg.hr_forum_webhook:
            webhook_url = f"{tcfg.hr_forum_webhook}?thread_id={ticket.discord_channel_id}"
            embed = {
                "title": "Ticket Comment",
                "description": message,
                "color": 0xFFA500  # Orange
            }
            send_message({"embeds": [embed]}, hook=webhook_url)

    ticket.is_resolved = True
    ticket.save(update_fields=["is_resolved"])


def close_char_removed_ticket(ticket, user=None):
    """Mark a char_removed ticket resolved without deleting it (legacy behavior)."""
    message = "✅ Ticket resolved"
    if user:
        main_char = getattr(user.profile, "main_character", None)
        name = main_char.character_name if main_char else user.username
        message += f" by **{name}**"

    add_ticket_comment(ticket, user, message, post_to_discord=True)

    ticket.is_resolved = True
    ticket.save(update_fields=["is_resolved"])


def reopen_ticket(ticket, user=None, message=None):
    """Reopen a resolved ticket and unarchive its Discord channel/thread."""
    if not message and user:
        main_char = getattr(user.profile, "main_character", None)
        name = main_char.character_name if main_char else user.username
        message = f"🔄 Ticket reopened by **{name}**."

    if message:
        add_ticket_comment(ticket, user, message, post_to_discord=True)

    ticket.is_resolved = False
    ticket.save(update_fields=["is_resolved"])
    if run_task_function and ticket.discord_channel_id:
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.unarchive_thread"],
            kwargs={
                "task_args": [ticket.discord_channel_id],
                "task_kwargs": {}
            },
            queue='aadiscordbot'
        )


def mark_ticket_exception(ticket, user, reason=None):
    """Mark a ticket as an exception and notify Discord."""
    ticket.is_exception = True
    ticket.exception_reason = reason or f"Marked as exception by {user.username}"
    ticket.save(update_fields=["is_exception", "exception_reason"])

    main_char = getattr(user.profile, "main_character", None)
    name = main_char.character_name if main_char else user.username
    msg = f"ℹ️ Ticket marked as exception by **{name}**."
    if reason:
        msg += f"\nReason: {reason}"

    add_ticket_comment(ticket, user, msg, post_to_discord=True)


def clear_ticket_exception(ticket, user):
    """Clear exception status and notify Discord."""
    ticket.is_exception = False
    ticket.exception_reason = None
    ticket.save(update_fields=["is_exception", "exception_reason"])

    main_char = getattr(user.profile, "main_character", None)
    name = main_char.character_name if main_char else user.username
    msg = f"🔄 Exception status cleared by **{name}**."

    add_ticket_comment(ticket, user, msg, post_to_discord=True)
