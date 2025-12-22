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

            from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
            from .tasks_utils import setup_periodic_task, format_task_name
            from .models import BigBrotherConfig

            # --- CLEANUP AND MIGRATION ---
            try:
                # 1. Delete obsolete Reddit tasks
                reddit_orphans = PeriodicTask.objects.filter(name__icontains="reddit")
                if reddit_orphans.exists():
                    logger.info(f"🗑️ [AA-BB] - [Apps] - Deleting {reddit_orphans.count()} obsolete reddit periodic tasks.")
                    reddit_orphans.delete()

                # 2. Rename existing tasks to the new naming scheme
                migration_filters = ["AA-BB: ", "BB ", "CB ", "tickets run"]
                for pattern in migration_filters:
                    for task in PeriodicTask.objects.filter(name__startswith=pattern):
                        new_name = format_task_name(task.name)
                        if new_name != task.name:
                            if PeriodicTask.objects.filter(name=new_name).exists():
                                existing = PeriodicTask.objects.get(name=new_name)
                                if existing.task == task.task:
                                    logger.info(f"🗑️ [AA-BB] - [Apps] - Deleting duplicate task '{task.name}' as '{new_name}' already exists.")
                                    task.delete()
                                    continue

                            logger.info(f"🔄 [AA-BB] - [Apps] - Renaming periodic task '{task.name}' -> '{new_name}'")
                            task.name = new_name
                            task.save()
            except Exception as e:
                logger.warning(f"⚠️ [AA-BB] - [Apps] - Task migration/cleanup failed: {e}")
            # --- END CLEANUP ---

            config = BigBrotherConfig.get_solo()
            stagger = max(getattr(config, "update_stagger_seconds", 3600), 3600)

            interval, _ = IntervalSchedule.objects.get_or_create(
                every=stagger,
                period=IntervalSchedule.SECONDS,
            )

            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute='25',
                hour='*',
                day_of_week='*',
                day_of_month='*',
                month_of_year='*',
                timezone='UTC',
            )

            setup_periodic_task(
                name="BB run regular updates",
                task_path="aa_bb.tasks.BB_run_regular_updates",
                schedule=interval,
                enabled=config.is_active
            )

            setup_periodic_task(
                name="CB run regular updates",
                task_path="aa_bb.tasks_cb.CB_run_regular_updates",
                schedule=schedule,
                enabled=config.is_active
            )

            # Optional: Sync standings from aa-contacts into BigBrother hostiles/members
            if apps.is_installed("aa_contacts"):
                setup_periodic_task(
                    name="BB sync contacts from aa-contacts",
                    task_path="aa_bb.tasks.BB_sync_contacts_from_aa_contacts",
                    schedule=schedule,
                    enabled=config.is_active
                )
            else:
                logger.info("ℹ️  [AA-BB] - [Apps] - aa_contacts not installed; skipping 'BB sync contacts from aa-contacts' beat task registration")

            setup_periodic_task(
                name="BB kickstart stale CT modules",
                task_path="aa_bb.tasks_ct.kickstart_stale_ct_modules",
                schedule=schedule,
                enabled=config.is_active
            )

            setup_periodic_task(
                name="tickets run regular updates",
                task_path="aa_bb.tasks_tickets.hourly_compliance_check",
                schedule=schedule,
                enabled=config.is_active
            )

            scheduleloa, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="12",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            setup_periodic_task(
                name="BB run regular LoA updates",
                task_path="aa_bb.tasks_cb.BB_run_regular_loa_updates",
                schedule=scheduleloa,
                enabled=config.is_loa_active
            )

            setup_periodic_task(
                name="BB check member compliance",
                task_path="aa_bb.tasks_cb.check_member_compliance",
                schedule=scheduleloa,
                enabled=config.is_paps_active
            )

            schedule_stats, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="12",
                day_of_week="0",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            setup_periodic_task(
                name="BB send recurring stats",
                task_path="aa_bb.tasks_other.BB_send_recurring_stats",
                schedule=schedule_stats,
                enabled=config.are_recurring_stats_active
            )

            scheduleDB, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="1",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            setup_periodic_task(
                name="BB run regular DB cleanup",
                task_path="aa_bb.tasks_cb.BB_daily_DB_cleanup",
                schedule=scheduleDB,
                enabled=config.is_active
            )


            # Daily messages
            config = BigBrotherConfig.get_solo()

            # Default fallback schedule (12:00 UTC daily)
            default_schedule, _ = CrontabSchedule.objects.get_or_create(
                minute='0',
                hour='12',
                day_of_week='*',
                day_of_month='*',
                month_of_year='*',
                timezone='UTC',
            )

            tasks = [
                {
                    "name": "BB send daily message",
                    "task_path": "aa_bb.tasks_cb.BB_send_daily_messages",
                    "schedule": getattr(config, "dailyschedule", None) or default_schedule,
                    "enabled": config.are_daily_messages_active,
                },
                {
                    "name": "BB send optional message 1",
                    "task_path": "aa_bb.tasks_cb.BB_send_opt_message1",
                    "schedule": getattr(config, "optschedule1", None) or default_schedule,
                    "enabled": config.are_opt_messages1_active,
                },
                {
                    "name": "BB send optional message 2",
                    "task_path": "aa_bb.tasks_cb.BB_send_opt_message2",
                    "schedule": getattr(config, "optschedule2", None) or default_schedule,
                    "enabled": config.are_opt_messages2_active,
                },
                {
                    "name": "BB send optional message 3",
                    "task_path": "aa_bb.tasks_cb.BB_send_opt_message3",
                    "schedule": getattr(config, "optschedule3", None) or default_schedule,
                    "enabled": config.are_opt_messages3_active,
                },
                {
                    "name": "BB send optional message 4",
                    "task_path": "aa_bb.tasks_cb.BB_send_opt_message4",
                    "schedule": getattr(config, "optschedule4", None) or default_schedule,
                    "enabled": config.are_opt_messages4_active,
                },
                {
                    "name": "BB send optional message 5",
                    "task_path": "aa_bb.tasks_cb.BB_send_opt_message5",
                    "schedule": getattr(config, "optschedule5", None) or default_schedule,
                    "enabled": config.are_opt_messages5_active,
                },
            ]

            for task_info in tasks:
                setup_periodic_task(
                    name=task_info["name"],
                    task_path=task_info["task_path"],
                    schedule=task_info["schedule"],
                    enabled=task_info["enabled"]
                )


        except (OperationalError, ProgrammingError, ImportError):
            pass
