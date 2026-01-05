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
    run_task_function = None
    get_discord_user_id = None
    logger.info("✅  [AA-BB] - [Signals] - aadiscordbot not installed, signaling will use fallbacks.")

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

        from .tasks_tickets import ensure_ticket
        ensure_ticket(instance.user, "char_removed", details=str(character))

    except Exception as e:
        logger.error("✅  [AA-BB] - [Signals] - Failed to create character-removed ticket: %s", e)
