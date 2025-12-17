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
from .tasks import BB_register_message_tasks
from .app_settings import send_message

import logging

logger = logging.getLogger(__name__)

try:
    from aadiscordbot.tasks import run_task_function
    from aadiscordbot.utils.auth import get_discord_user_id
except ImportError:
    logger.error("aadiscordbot not installed, signaling won't work.")

@receiver(post_save, sender=BigBrotherConfig)
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
        member_states = BigBrotherConfig.get_solo().bb_member_states.all()
        if instance.user.profile.state not in member_states:
            return
        tcfg = TicketToolConfig.get_solo()
        ticket_message = (
            f"<@&{tcfg.Role_ID}>,<@{discord_id}> Auth lost access to your character "
            f"{character}, this happens when the token used expires, which usually happens "
            f"when you change your PW. Please fix it ASAP and get yourself a PW manager so "
            f"you don't forget it again. (you'll need to do so on all 3 auths)"
        )
        send_message(f"ticket for {instance.user} created, reason - Character Removed")
        run_task_function.apply_async(
            args=["aa_bb.tasks_bot.create_compliance_ticket"],
            kwargs={
                "task_args": [instance.user.id, discord_id, "char_removed", ticket_message],
                "task_kwargs": {},
            },
        )

    except Exception as e:
        logger.error("Failed to create character-removed ticket: %s", e)
