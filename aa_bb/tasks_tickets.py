"""Celery tasks and helpers that manage compliance tickets and reminders."""

import logging
logger = logging.getLogger(__name__)

from typing import Optional

from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model

from celery import shared_task

from allianceauth.authentication.models import UserProfile
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.modules.discord.models import DiscordUser

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

from .models import BigBrotherConfig, TicketToolConfig, PapCompliance, LeaveRequest, ComplianceTicket

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

    # Find the most recent logoff among all alts
    latest_logoff = None
    for char in get_alts_queryset(ec):
        audit = getattr(char, "characteraudit", None)
        ts = getattr(audit, "last_known_logoff", None) if audit else None
        if ts and (latest_logoff is None or ts > latest_logoff):  # Track the newest timestamp.
            latest_logoff = ts

    if not latest_logoff:  # No logoff information means fail the AFK check.
        return False

    # Compute days since that logoff
    days_since = (timezone.now() - latest_logoff).days
    if days_since >= max_afk_days:  # Too many days inactive triggers failure.
        return False
    return True


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
    if not discordbot_active() or not tcfg.discord_inactivity_enabled:
        return True

    from .models import UserStatus
    status, _ = UserStatus.objects.get_or_create(user=user)

    if not status.last_discord_message_at:
        # If no message recorded, they might be inactive or we just started tracking.
        # Initialize to now to avoid mass ticketing on feature activation.
        try:
            get_discord_user_id(user)
            status.last_discord_message_at = timezone.now()
            status.save(update_fields=['last_discord_message_at'])
            return True
        except Exception:
            # Not on Discord, skip this check
            return True

    days_since = (timezone.now() - status.last_discord_message_at).days
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


@shared_task
def hourly_compliance_check():
    """Run the top-of-hour audit that enforces compliance rules and reminders."""
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

    profiles = list(profiles_qs)
    allowed_users = {p.user for p in profiles}

    # 1. Check compliance reasons
    for UserProfil in profiles:
        user = UserProfil.user
        if user in t_cfg.excluded_users.all():  # Skip users explicitly excluded from checks.
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

    tickets_qs = ComplianceTicket.objects.filter(is_resolved=False)
    if bb_cfg.limit_to_main_corp:
        tickets_qs = tickets_qs.filter(user__profile__main_character__corporation_id=bb_cfg.main_corporation_id)

    for ticket in tickets_qs:
        reason = ticket.reason
        hook = get_webhook_for_reason(reason)

        if reason == "char_removed" or reason == "awox_kill":
            # These rely on manual resolution flow and currently have no automated reminders
            continue

        checker, _ = reason_checkers[reason]

        # resolved?
        if ticket.user and checker(ticket.user):  # Condition cleared, close and notify.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"✅ Ticket for <@{ticket.discord_user_id}> (**{reason}**) resolved")
            continue

        if ticket.user not in allowed_users:  # User left the org, close ticket and alert.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"❌ User <@{ticket.discord_user_id}> is no longer a member, closing ticket (**{reason}**)")
            continue

        if not ticket.user:  # Missing auth user entirely, close ticket.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                add_notification(hook, f"⚠️ Ticket for <@{ticket.discord_user_id}> (**{reason}**) closed due to missing auth user")
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
        mention = f"{ticket.discord_user_id}"
        template = reminder_messages[reason]  # must support {namee}, {role}, {days}
        if reason == "paps_check":  # PAP reminder template only uses {days}.
            msg = template.format(days=days_left)
        else:
            msg = template.format(namee=mention, role=t_cfg.Role_ID, days=days_left)

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
        if not include_user:
            msg_template = msg_template.replace("<@{namee}>", "{namee}")

        namee_val = discord_id if include_user else user.username

        if reason == "afk_check":  # AFK templates expect {days}.
            ticket_message = msg_template.format(namee=namee_val, role=tcfg.Role_ID, days=max_afk_days)
        elif reason == "discord_inactivity":
            ticket_message = msg_template.format(namee=namee_val, role=tcfg.Role_ID, days=tcfg.discord_inactivity_days)
        elif reason == "discord_check":  # Discord-specific template uses username, not Discord mention.
            username = user.username
            ticket_message = msg_template.format(namee=username, role=tcfg.Role_ID, days=max_afk_days)
        elif reason in ["char_removed", "awox_kill"]:
            ticket_message = msg_template.format(namee=namee_val, role=tcfg.Role_ID, details=details)
        else:
            ticket_message = msg_template.format(namee=namee_val, role=tcfg.Role_ID)
    except NotAuthenticated:
        # User has no Discord → fall back to first superuser with Discord linked
        superusers = User.objects.filter(is_superuser=True)
        username = user.username
        discord_user = None

        # Prefer a superuser with a linked Discord account
        if superusers.exists():  # Only check DiscordUser table when any superuser exists.
            discord_user = DiscordUser.objects.filter(user__in=superusers).first()

        # If no superuser exists or none have Discord linked, try the first configured Discord admin
        if not discord_user:  # Fallback to admins defined in aadiscordbot settings.
            try:
                admin_uids = get_admins() or []
            except Exception:
                admin_uids = []

            if admin_uids:  # Only query DiscordUser when admin list is non-empty.
                discord_user = DiscordUser.objects.filter(uid__in=admin_uids).first()

        # If still nothing, log and notify, then stop
        if not discord_user:  # There is no reasonable recipient—alert staff and bail.
            logger.error(f"✅  [AA-BB] - [ensure_ticket] - Failed to create a {reason} ticket for {username}. No eligible fallback found: no superuser or Discord admin with Discord linked.")
            send_status_embed(
                subject="Ticket Creation Failed",
                lines=[f"Failed to create a **{reason}** ticket for **{username}**. No eligible fallback found: no superuser or Discord admin with Discord linked."],
                color=0xe74c3c,  # Red
                hook=get_webhook_for_reason(reason)
            )
            return

        discord_id = discord_user.uid
        _, msg_template = reason_checkers[reason]
        if reason == "afk_check":  # Fallback message includes manual warning text.
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, days=max_afk_days)}"
            )
        elif reason == "discord_inactivity":
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord activity!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, days=tcfg.discord_inactivity_days)}"
            )
        elif reason == "discord_check":  # Discord issues share the same format as AFK fallback.
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, days=max_afk_days)}"
            )
        elif reason in ["char_removed", "awox_kill"]:
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, details=details)}"
            )
        else:
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID)}"
            )

    # prevent duplicates
    exists = ComplianceTicket.objects.filter(
        user=user, reason=reason, is_resolved=False
    ).exists()
    if not exists:  # Only emit side effects when a new ticket is needed.
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
                ticket_id=tcfg.ticket_counter
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
                ticket_id=tcfg.ticket_counter
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
                    "task_kwargs": {"thread_id": thread_id, "include_user": include_user}
                },
                queue='aadiscordbot'
            )

        elif ticket_type == TicketToolConfig.TICKET_TYPE_PRIVATE_CHANNEL:
            # Legacy Private Channel
            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.create_compliance_ticket"],
                kwargs={
                    "task_args": [user.id, discord_id, reason, ticket_message],
                    "task_kwargs": {"include_user": include_user}
                },
                queue='aadiscordbot'
            )


def close_ticket(ticket):
    """Close the Discord compliance ticket and mark it resolved locally."""
    if run_task_function and ticket.discord_channel_id:
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.close_ticket_channel"],
            kwargs={
                "task_args": [ticket.discord_channel_id],
                "task_kwargs": {}
            },
            queue='aadiscordbot'
        )
    ticket.is_resolved = True
    ticket.save(update_fields=["is_resolved"])


def close_char_removed_ticket(ticket):
    """Mark a char_removed ticket resolved without deleting it (legacy behavior)."""
    ticket.is_resolved = True
    ticket.save()
