"""
AppConfig bootstrap for aa_bb.

The AppConfig ensures Django wires up signals, celery tasks, message types,
and periodic scheduler entries as soon as the app loads.
"""

from django.apps import AppConfig, apps
from django.db.utils import OperationalError, ProgrammingError
from django.db import IntegrityError, transaction

class AaBbConfig(AppConfig):
    """App bootstrap that wires signals, tasks, and beat entries."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "aa_bb"
    verbose_name = "aa_bb"

    def ready(self):
        """Register signals and ensure Celery beat tasks/message types exist."""
        import aa_bb.signals
        import logging
        from django.db.utils import OperationalError, ProgrammingError
        logger = logging.getLogger(__name__)
        from .models import MessageType
        from allianceauth.authentication.models import State

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

        try:
            for msg_name in PREDEFINED_MESSAGE_TYPES:
                obj, created = MessageType.objects.get_or_create(name=msg_name)
                if created:  # Log whenever a predefined message type is inserted.
                    logger.info(f"✅  [AA-BB] - [Apps] - Added predefined MessageType: {msg_name}")
        except (OperationalError, ProgrammingError):
            # Database isn't ready (e.g., before migrations)
            logger.info(f"ℹ️  [AA-BB] - [Apps] - Database not ready yet, skipping MessageType registration.")
            pass

        try:
            from django.db import connection
            if "aa_bb_bigbrotherconfig" not in connection.introspection.table_names():
                return

            from .tasks_utils import sync_periodic_tasks
            sync_periodic_tasks()
        except (OperationalError, ProgrammingError, ImportError):
            pass
