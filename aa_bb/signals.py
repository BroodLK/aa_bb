"""
Django signal handlers used by BigBrother.

Currently:
1. When the singleton config is saved, Celery message tasks stay in sync.
2. When a character ownership is deleted, optionally open a compliance ticket.
"""

from django.dispatch import receiver
from django.db.models.signals import post_save, pre_delete

from allianceauth.authentication.models import CharacterOwnership

from .models import BigBrotherConfig, TicketToolConfig
from .tasks_cb import BB_register_message_tasks
from .tasks_tickets import get_webhook_for_reason
from .app_settings import send_message, send_status_embed

import logging

logger = logging.getLogger(__name__)

try:
    from aadiscordbot.tasks import run_task_function
    from aadiscordbot.utils.auth import get_discord_user_id
except ImportError:
    logger.error("✅  [AA-BB] - [Signals] - aadiscordbot not installed, signaling won't work.")

@receiver(post_save, sender=BigBrotherConfig)
@receiver(post_save, sender=TicketToolConfig)
def trigger_task_sync(sender, instance, **kwargs):
    """When the config changes, make sure Celery schedules match the DB."""
    BB_register_message_tasks.delay()


@receiver(pre_delete, sender=CharacterOwnership)
def removed_character(sender, instance, **kwargs):
    """
    If the ticket tool is monitoring “character removed” events, raise a ticket
    any time Auth loses access to one of the pilot’s characters.
    """
    if not TicketToolConfig.get_solo().char_removed_enabled:
        return
    try:
        character = instance.character
        discord_id = get_discord_user_id(instance.user)
        bb_cfg = BigBrotherConfig.get_solo()
        member_states = bb_cfg.bb_member_states.all()
        if instance.user.profile.state not in member_states:
            return

        if bb_cfg.limit_to_main_corp:
            # Check if the user's main character belongs to the primary corporation
            profile = getattr(instance.user, 'profile', None)
            main_char = getattr(profile, 'main_character', None) if profile else None
            if not main_char or main_char.corporation_id != bb_cfg.main_corporation_id:
                return

        tcfg = TicketToolConfig.get_solo()
        ticket_message = (
            f"<@&{tcfg.Role_ID}>,<@{discord_id}> Auth lost access to your character "
            f"{character}, this happens when the token used expires, which usually happens "
            f"when you change your PW. Please fix it ASAP and get yourself a PW manager so "
            f"you don't forget it again. (you'll need to do so on all 3 auths)"
        )
        send_status_embed(
            subject="Ticket Created",
            lines=[f"Ticket for **{instance.user}** created, reason - **Character Removed**"],
            color=0xf1c40f,  # Yellow
            hook=get_webhook_for_reason("char_removed")
        )
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.create_compliance_ticket"],
            kwargs={
                "task_args": [instance.user.id, discord_id, "char_removed", ticket_message],
                "task_kwargs": {},
            },
        )

    except Exception as e:
        logger.error("✅  [AA-BB] - [Signals] - Failed to create character-removed ticket: %s", e)
