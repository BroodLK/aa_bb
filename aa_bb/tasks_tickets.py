"""Celery tasks and helpers that manage compliance tickets and reminders."""

import logging
logger = logging.getLogger(__name__)

from typing import Optional

from django.utils import timezone
from django.contrib.auth import get_user_model

from celery import shared_task

from allianceauth.authentication.models import UserProfile
from allianceauth.eveonline.models import EveCharacter
from allianceauth.services.modules.discord.models import DiscordUser

try:
    from aadiscordbot.tasks import run_task_function
    from aadiscordbot.utils.auth import get_discord_user_id
    from aadiscordbot.cogs.utils.exceptions import NotAuthenticated
    from aadiscordbot.app_settings import get_admins
except ImportError:
    logger.error("aadiscordbot not installed; compliance checks will not work.")
    run_task_function = None
    get_discord_user_id = None

    class NotAuthenticated(Exception):
        pass
    get_admins = None

try:
    from corptools.api.helpers import get_alts_queryset
except ImportError:
    logger.error("corptools not installed; compliance checks will not work.")

    def get_alts_queryset(*args, **kwargs):
        return []

from .models import BigBrotherConfig, TicketToolConfig, PapCompliance, LeaveRequest, ComplianceTicket
from .app_settings import send_message, get_user_profiles, get_character_id

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
        logger.warning("TicketToolConfig.get_solo() failed; treating user as compliant.")
        return True

    if not cfg or not cfg.compliance_filter:  # Missing configuration leaves everyone compliant.
        return True

    try:
        # process_filter(user) returns the 'check' boolean for this user,
        # where 'check' already applies the filter and the 'negate' flag.
        return bool(cfg.compliance_filter.process_filter(user))
    except Exception:
        # Misconfiguration or unexpected error: log and be lenient.
        logger.exception("Error while running compliance filter for user id=%s", user.id)
        return True


def paps_check(user):
    """
    Inspect PAP compliance state for the user, honoring LoA in-progress status.

    Returns True when PAP checks are disabled, the user has an LoA pending, or
    their PapCompliance row indicates compliance.
    """
    if not TicketToolConfig.get_solo().paps_check_enabled:  # Globally disabled -> compliant.
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
    if not TicketToolConfig.get_solo().discord_check_enabled:  # Disabled toggle permits everyone.
        return True
    try:
        discord_id = get_discord_user_id(user)
    except NotAuthenticated:
        return False  # Missing Discord auth fails the check.
    return True



@shared_task
def hourly_compliance_check():
    """Run the top-of-hour audit that enforces compliance rules and reminders."""
    cfg = TicketToolConfig.get_solo()
    max_days = {
        "corp_check": cfg.corp_check,
        "paps_check": cfg.paps_check,
        "afk_check": cfg.afk_check,
        "discord_check": cfg.discord_check,
    }

    # Per-reason reminder frequency (in days)
    reminder_frequency = {
        "corp_check": cfg.corp_check_frequency,
        "paps_check": cfg.paps_check_frequency,
        "afk_check": cfg.afk_check_frequency,
        "discord_check": cfg.discord_check_frequency,
    }

    reason_checkers = {
        "corp_check": (corp_check, cfg.corp_check_reason),
        "paps_check": (paps_check, cfg.paps_check_reason),
        "afk_check": (afk_check, cfg.afk_check_reason),
        "discord_check": (discord_check, cfg.discord_check_reason),
    }

    reminder_messages = {
        "corp_check": cfg.corp_check_reminder,
        "paps_check": cfg.paps_check_reminder,
        "afk_check": cfg.afk_check_reminder,
        "discord_check": cfg.discord_check_reminder,
    }

    now = timezone.now()

    profiles = list(get_user_profiles())
    allowed_users = {p.user for p in profiles}

    # 1. Check compliance reasons
    for UserProfil in get_user_profiles():
        user = UserProfil.user
        if user in cfg.excluded_users.all():  # Skip users explicitly excluded from checks.
            continue
        for reason, (checker, msg_template) in reason_checkers.items():
            checked = checker(user)
            if not checked:  # Non-compliant result requires a ticket/ensuring existing one.
                logger.info(f"user{user},reason{reason},checked{checked}")
                ensure_ticket(user, reason)

    # 2. Process existing tickets
    ticket_resolved_manually_notify = BigBrotherConfig.ticket_notify_man
    ticket_resolved_automatic_notify = BigBrotherConfig.ticket_notify_auto
    for ticket in ComplianceTicket.objects.all():
        reason = ticket.reason

        if reason == "char_removed" or reason == "awox_kill":  # These rely on manual resolution flow.
            logger.info(f"reason:{reason}, resolved:{ticket.is_resolved}")
            if ticket.is_resolved:  # Completed ticket can be closed out and announced.
                logger.info(f"reason:{reason}")
                close_ticket(ticket)
                if ticket_resolved_manually_notify:
                    send_message(f"ticket for <@{ticket.discord_user_id}> resolved")
            continue

        checker, _ = reason_checkers[reason]

        # resolved?
        if ticket.user and checker(ticket.user):  # Condition cleared, close and notify.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                send_message(f"ticket for <@{ticket.discord_user_id}> resolved")
            continue

        if ticket.user not in allowed_users:  # User left the org, close ticket and alert.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                send_message(f"User <@{ticket.discord_user_id}> is no longer a member, closing ticket")
            continue

        if not ticket.user:  # Missing auth user entirely, close ticket.
            close_ticket(ticket)
            if ticket_resolved_automatic_notify:
                send_message(f"ticket for <@{ticket.discord_user_id}> closed due to missing auth user")
            continue

        # Reminder logic with per-reason frequency + max-days cap
        days_elapsed = (now - ticket.created_at).days
        if days_elapsed <= 0:  # Do not send reminders on the same day ticket was created.
            continue  # don't ping on creation day

        max_dayss = max_days.get(reason, 30)
        if days_elapsed > max_dayss:  # Escalate when overdue beyond max window.
            # escalation: ping staff role to kick the user
            mention = f"<@&{cfg.Role_ID}>"           # role mention
            user_mention = f"<@{ticket.discord_user_id}>"
            msg = (f"⚠️ {mention} please review compliance ticket for {user_mention}. "
                   f"Issue **{reason}** has exceeded {max_dayss} days without resolution. "
                   f"Consider kicking this user.")

            run_task_function.apply_async(
                args=["aa_bb.tasks_bot.send_ticket_reminder"],
                kwargs={
                    "task_args": [ticket.discord_channel_id, ticket.discord_user_id, msg],
                    "task_kwargs": {}
                }
            )
            continue

        # last_reminder_sent stores the last day number that was pinged
        freq_days = reminder_frequency.get(reason, 1)
        last_day_pinged = ticket.last_reminder_sent or 0
        if (days_elapsed - last_day_pinged) < freq_days:  # Respect reminder spacing.
            continue  # not time to remind yet

        # Build the message: mention the user + role + days left
        days_left = max_dayss - days_elapsed
        mention = f"{ticket.discord_user_id}"
        template = reminder_messages[reason]  # must support {namee}, {role}, {days}
        if reason == "paps_check":  # PAP reminder template only uses {days}.
            msg = template.format(days=days_left)
        else:
            msg = template.format(namee=mention, role=cfg.Role_ID, days=days_left)

        # Queue the bot-side reminder (ensure task_kwargs is present)
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


def ensure_ticket(user, reason):
    """
    Guarantee there is an open compliance ticket for the given user/reason pair.

    Handles Discord lookup, fallbacks, and message templating before delegating
    the actual ticket creation to the bot worker.
    """
    tcfg = TicketToolConfig.get_solo()
    max_afk_days = tcfg.Max_Afk_Days
    reason_checkers = {
        "corp_check": (corp_check, tcfg.corp_check_reason),
        "paps_check": (paps_check, tcfg.paps_check_reason),
        "afk_check": (afk_check, tcfg.afk_check_reason),
        "discord_check": (discord_check, tcfg.discord_check_reason),
    }
    try:
        discord_id = get_discord_user_id(user)
        username = ""
        _, msg_template = reason_checkers[reason]
        if reason == "afk_check":  # AFK templates expect {days}.
            ticket_message = msg_template.format(namee=discord_id, role=tcfg.Role_ID, days=max_afk_days)
        elif reason == "discord_check":  # Discord-specific template uses username, not Discord mention.
            username = user.username
            ticket_message = msg_template.format(namee=username, role=tcfg.Role_ID, days=max_afk_days)
        else:
            ticket_message = msg_template.format(namee=discord_id, role=tcfg.Role_ID)
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
            logger.error(f"Failed to create a {reason} ticket for {username}. No eligible fallback found: no superuser or Discord admin with Discord linked.")
            send_message(f"Failed to create a {reason} ticket for {username}. No eligible fallback found: no superuser or Discord admin with Discord linked.")
            return

        discord_id = discord_user.uid
        _, msg_template = reason_checkers[reason]
        if reason == "afk_check":  # Fallback message includes manual warning text.
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, days=max_afk_days)}"
            )
        elif reason == "discord_check":  # Discord issues share the same format as AFK fallback.
            ticket_message = (
                f"⚠️ Compliance issue for **{user.username}** "
                f"(no Discord linked!)\n\n"
                f"{msg_template.format(namee=user.username, role=tcfg.Role_ID, days=max_afk_days)}"
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
        send_message(f"ticket for {user.username} created, reason - {reason}")
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.create_compliance_ticket"],
            kwargs={
                "task_args": [user.id, discord_id, reason, ticket_message],
                "task_kwargs": {}
            },
            queue='aadiscordbot'
        )


def close_ticket(ticket):
    """Close the Discord compliance ticket and delete it locally."""
    run_task_function.apply_async(
        args=["aa_bb.tasks_bot.close_ticket_channel"],
        kwargs={
            "task_args": [ticket.discord_channel_id],
            "task_kwargs": {}
        },
        queue='aadiscordbot'
    )
    ticket.delete()


def close_char_removed_ticket(ticket):
    """Mark a char_removed ticket resolved without deleting it (legacy behavior)."""
    ticket.is_resolved = True
    ticket.save()
