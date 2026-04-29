"""
AppConfig bootstrap for aa_bb.

Database-backed bootstrap work is deferred until after migrations so Django 5
does not warn about database access during app initialization.
"""

import logging

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError
from django.db.models.signals import post_migrate


logger = logging.getLogger(__name__)

PREDEFINED_MESSAGE_TYPES = [
    "LoA Request",
    "LoA Changed Status",
    "LoA Inactivity",
    "New Version",
    "Error",
    "AwoX",
    "Can Light Cyno",
    "Cyno Update",
    "New Hostile Assets",
    "New Hostile Clones",
    "New Sus Contacts",
    "New Sus Contracts",
    "New Sus Mails",
    "New Sus Transactions",
    "New Blacklist Entry",
    "skills",
    "All Cyno Changes",
    "Compliance",
    "SP Injected",
    "Omega Detected",
]


def bootstrap_runtime_state(**kwargs):
    """
    Ensure aa_bb's DB-backed runtime records exist once migrations are complete.
    """
    from .models import MessageType
    from .tasks_utils import sync_periodic_tasks

    try:
        for msg_name in PREDEFINED_MESSAGE_TYPES:
            obj, created = MessageType.objects.get_or_create(name=msg_name)
            if created:
                logger.info("Added predefined MessageType: %s", msg_name)

        sync_periodic_tasks()
    except (OperationalError, ProgrammingError):
        logger.info("Database not ready yet, skipping aa_bb runtime bootstrap.")


class AaBbConfig(AppConfig):
    """App bootstrap that wires signals and defers DB setup until post-migrate."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "aa_bb"
    verbose_name = "aa_bb"

    def ready(self):
        import aa_bb.signals  # noqa: F401

        post_migrate.connect(
            bootstrap_runtime_state,
            sender=self,
            dispatch_uid="aa_bb.bootstrap_runtime_state",
        )
