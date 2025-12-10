"""
AppConfig bootstrap for aa_bb.

The AppConfig ensures Django wires up signals, celery tasks, message types,
and periodic scheduler entries as soon as the app loads.
"""

from django.apps import AppConfig
from django.db.utils import OperationalError, ProgrammingError

class AaBbConfig(AppConfig):
    """App bootstrap that wires signals, tasks, and beat entries."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "aa_bb"
    verbose_name = "aa_bb"

    def ready(self):
        """Register signals and ensure Celery beat tasks/message types exist."""
        import aa_bb.signals
        import aa_bb.tasks_reddit  # noqa: F401  # ensure Celery auto-discovery
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

        state_names = list(State.objects.values_list("name", flat=True))

        try:
            for msg_name in PREDEFINED_MESSAGE_TYPES:
                obj, created = MessageType.objects.get_or_create(name=msg_name)
                if created:  # Log whenever a predefined message type is inserted.
                    logger.info(f"✅ Added predefined MessageType: {msg_name}")
        except (OperationalError, ProgrammingError):
            # Database not ready (e.g., during migrate)
            pass

        try:
            from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule

            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute='25',
                hour='*',
                day_of_week='*',
                day_of_month='*',
                month_of_year='*',
                timezone='UTC',
            )

            task, created = PeriodicTask.objects.get_or_create(
                name="BB run regular updates",
                defaults={
                    "crontab": schedule,
                    "task": "aa_bb.tasks.BB_run_regular_updates",
                    "enabled": False,  # only on creation
                },
            )

            if not created:  # Existing record found; ensure configuration matches expectations.
                updated = False
                if task.interval:
                    task.interval = None
                    updated = True
                if task.crontab != schedule or task.task != "aa_bb.tasks.BB_run_regular_updates":  # Ensure interval/task stay canonical.
                    # Realign interval/task bindings to the expected values.
                    task.crontab = schedule
                    task.task = "aa_bb.tasks.BB_run_regular_updates"
                    updated = True
                if updated:  # Surface when the periodic task required modification.
                    task.save()
                    logger.info("✅ Updated ‘BB run regular updates’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB run regular updates’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘BB run regular updates’ periodic task with enabled=False")

            task_cb, created_cb = PeriodicTask.objects.get_or_create(
                name="CB run regular updates",
                defaults={
                    "crontab": schedule,
                    "task": "aa_bb.tasks_cb.CB_run_regular_updates",
                    "enabled": False,  # only on creation
                },
            )

            if not created_cb:  # Existing CorpBrother task detected.
                updated_cb = False
                if task_cb.interval:
                    task_cb.interval = None
                    updated_cb = True
                if task_cb.crontab != schedule or task_cb.task != "aa_bb.tasks_cb.CB_run_regular_updates":  # Keep CB task mapping aligned.
                    # Bring the CB task settings back to the canonical values.
                    task_cb.crontab = schedule
                    task_cb.task = "aa_bb.tasks_cb.CB_run_regular_updates"
                    updated_cb = True
                if updated_cb:  # Only log when changes were saved.
                    task_cb.save()
                    logger.info("✅ Updated ‘CB run regular updates’ periodic task")
                else:
                    logger.info("ℹ️ ‘CB run regular updates’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘CB run regular updates’ periodic task with enabled=False")

            task_ct, created_ct = PeriodicTask.objects.get_or_create(
                name="BB kickstart stale CT modules",
                defaults={
                    "crontab": schedule,
                    "task": "aa_bb.tasks_ct.kickstart_stale_ct_modules",
                    "enabled": False,  # only on creation
                },
            )

            if not created_ct:  # Existing kickstart task found; ensure it matches defaults.
                updated_ct = False
                # Clear interval if set
                if task_ct.interval is not None:  # Force crontab mode by clearing stale interval assignments.
                    task_ct.interval = None
                    updated_ct = True
                if task_ct.crontab != schedule or task_ct.task != "aa_bb.tasks_ct.kickstart_stale_ct_modules":  # Align interval/task fields.
                    task_ct.crontab = schedule
                    task_ct.task = "aa_bb.tasks_ct.kickstart_stale_ct_modules"
                    updated_ct = True
                if updated_ct:  # Report when the stored task required tweaks.
                    task_ct.save()
                    logger.info("✅ Updated ‘BB kickstart stale CT modules’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB kickstart stale CT modules’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘BB kickstart stale CT modules’ periodic task with enabled=False")

            task_tickets, created_tickets = PeriodicTask.objects.get_or_create(
                name="tickets run regular updates",
                defaults={
                    "crontab": schedule,
                    "task": "aa_bb.tasks_tickets.hourly_compliance_check",
                    "enabled": False,  # only on creation
                },
            )

            if not created_tickets:  # Tickets beat already exists; keep it in sync.
                updated_tickets = False
                if task_tickets.interval:
                    task_tickets.interval = None
                    updated_tickets = True
                if task_tickets.crontab != schedule or task_tickets.task != "aa_bb.tasks_tickets.hourly_compliance_check":  # Align interval/task fields.
                    task_tickets.crontab = schedule
                    task_tickets.task = "aa_bb.tasks_tickets.hourly_compliance_check"
                    updated_tickets = True
                if updated_tickets:  # Log when configuration drift was corrected.
                    task_tickets.save()
                    logger.info("✅ Updated 'tickets run regular updates’ periodic task")
                else:
                    logger.info("ℹ️ ‘tickets run regular updates’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘tickets run regular updates’ periodic task with enabled=False")

            scheduleloa, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="12",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            task_loa, created_loa = PeriodicTask.objects.get_or_create(
                name="BB run regular LoA updates",
                defaults={
                    "crontab": scheduleloa,
                    "task": "aa_bb.tasks_cb.BB_run_regular_loa_updates",
                    "enabled": True,  # only on creation
                },
            )

            if not created_loa:  # Existing LoA beat entry; validate scheduling.
                updated_loa = False
                # Clear interval if set
                if task_loa.interval is not None:  # LoA task should rely on crontab, so clear intervals.
                    task_loa.interval = None
                    updated_loa = True
                if task_loa.crontab != scheduleloa or task_loa.task != "aa_bb.tasks_cb.BB_run_regular_loa_updates":  # Enforce canonical LoA schedule/task.
                    task_loa.crontab = scheduleloa
                    task_loa.task = "aa_bb.tasks_cb.BB_run_regular_loa_updates"
                    task_loa.save()
                    updated_loa = True
                if updated_loa:  # Emit when LoA settings were adjusted.
                    logger.info("✅ Updated ‘BB run regular LoA updates’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB run regular LoA updates’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘BB run regular LoA updates’ periodic task with enabled=False")

            task_comp, created_comp = PeriodicTask.objects.get_or_create(
                name="BB check member compliance",
                defaults={
                    "crontab": scheduleloa,
                    "task": "aa_bb.tasks_cb.check_member_compliance",
                    "enabled": False,  # only on creation
                },
            )

            if not created_comp:  # Found existing compliance beat entry; align it.
                updated_comp = False
                # Clear interval if set
                if task_comp.interval is not None:  # Compliance task should be crontab-driven.
                    task_comp.interval = None
                    updated_comp = True
                if task_comp.crontab != scheduleloa or task_comp.task != "aa_bb.tasks_cb.check_member_compliance":  # Ensure scheduling matches configuration.
                    task_comp.crontab = scheduleloa
                    task_comp.task = "aa_bb.tasks_cb.check_member_compliance"
                    task_comp.save()
                    updated_comp = True
                if updated_comp:  # Notify when compliance beat was updated.
                    logger.info("✅ Updated ‘BB check member compliance’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB check member compliance’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘BB check member compliance’ periodic task with enabled=False")

            schedule_stats, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="12",
                day_of_week="0",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            task_stats, created_stats = PeriodicTask.objects.get_or_create(
                name="BB send recurring stats",
                defaults={
                    "crontab": schedule_stats,
                    "task": "aa_bb.tasks_other.BB_send_recurring_stats",
                    "enabled": False,  # only on creation
                },
            )

            if not created_stats:
                updated_stats = False
                if task_stats.interval is not None:
                    task_stats.interval = None
                    updated_stats = True
                if task_stats.crontab != schedule_stats or task_stats.task != "aa_bb.tasks_other.BB_send_recurring_stats":
                    task_stats.crontab = schedule_stats
                    task_stats.task = "aa_bb.tasks_other.BB_send_recurring_stats"
                    task_stats.save()
                    updated_stats = True
                if updated_stats:
                    logger.info("✅ Updated ‘BB send recurring stats’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB send recurring stats’ periodic task already exists and is up to date")
            else:
                    logger.info("✅ Created ‘BB send recurring stats’ periodic task with enabled=False")



            scheduleDB, _ = CrontabSchedule.objects.get_or_create(
                minute="0",
                hour="1",
                day_of_week="*",
                day_of_month="*",
                month_of_year="*",
                timezone="UTC",
            )

            task_DB, created_DB = PeriodicTask.objects.get_or_create(
                name="BB run regular DB cleanup",
                defaults={
                    "crontab": scheduleDB,
                    "task": "aa_bb.tasks_cb.BB_daily_DB_cleanup",
                    "enabled": True,  # only on creation
                },
            )

            if not created_DB:  # Existing DB cleanup task, so reconcile settings.
                updated_DB = False
                # Clear interval if set
                if task_DB.interval is not None:  # Cleanup task must be crontab-driven.
                    task_DB.interval = None
                    updated_DB = True
                if task_DB.crontab != scheduleDB or task_DB.task != "aa_bb.tasks_cb.BB_daily_DB_cleanup":  # Keep task schedule consistent.
                    task_DB.crontab = scheduleDB
                    task_DB.task = "aa_bb.tasks_cb.BB_daily_DB_cleanup"
                    task_DB.save()
                    updated_DB = True
                if updated_DB:  # Provide feedback when the DB task was changed.
                    logger.info("✅ Updated ‘BB run regular DB cleanup’ periodic task")
                else:
                    logger.info("ℹ️ ‘BB run regular DB cleanup’ periodic task already exists and is up to date")
            else:
                logger.info("✅ Created ‘BB run regular DB cleanup’ periodic task with enabled=False")


            # Daily messages
            from .models import BigBrotherConfig
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

                if is_active:  # Only install/update tasks when the DLC toggle is active.
                    if existing_task is None:  # Nothing scheduled yet, so create it.
                        # Create new periodic task
                        PeriodicTask.objects.create(
                            name=name,
                            task=task_path,
                            crontab=schedule,
                            enabled=True,
                        )
                        logger.info(f"✅ Created '{name}' periodic task with enabled=True")
                    else:
                        updated = False
                        if existing_task.crontab != schedule:  # Update schedule when the config changed.
                            existing_task.crontab = schedule
                            updated = True
                        if existing_task.task != task_path:  # Ensure the task points to the correct callable.
                            existing_task.task = task_path
                            updated = True
                        if not existing_task.enabled:  # Reactivate disabled tasks when the feature toggles on.
                            existing_task.enabled = True
                            updated = True
                        if updated:  # Only hit the DB/logs when an entry was adjusted.
                            existing_task.save()
                            logger.info(f"✅ Updated '{name}' periodic task")
                        else:
                            logger.info(f"ℹ️ '{name}' periodic task already exists and is up to date")
                else:
                    # Not active - delete the task if exists
                    if existing_task:  # Remove stale beat entries when the feature is off.
                        existing_task.delete()
                        logger.info(f"🗑️ Deleted '{name}' periodic task because messages are disabled")

            from .reddit import is_reddit_module_visible

            reddit_task_names = [
                "BB reddit evejobs post",
                "BB reddit reply watcher",
            ]

            if is_reddit_module_visible():  # Only schedule reddit beat tasks when the module is allowed.
                reddit_post_schedule, _ = CrontabSchedule.objects.get_or_create(
                    minute="0",
                    hour="13",
                    day_of_week="*",
                    day_of_month="*",
                    month_of_year="*",
                    timezone="UTC",
                )

                reddit_post_task, created_reddit_post = PeriodicTask.objects.get_or_create(
                    name="BB reddit evejobs post",
                    defaults={
                        "crontab": reddit_post_schedule,
                        "task": "aa_bb.tasks_reddit.post_reddit_recruitment",
                        "enabled": False,
                    },
                )
                if not created_reddit_post:  # Task exists; keep schedule and callable synced.
                    updated = False
                    if reddit_post_task.crontab != reddit_post_schedule:  # Update schedule when configuration changed.
                        reddit_post_task.crontab = reddit_post_schedule
                        updated = True
                    if reddit_post_task.task != "aa_bb.tasks_reddit.post_reddit_recruitment":  # Ensure task path is up to date.
                        reddit_post_task.task = "aa_bb.tasks_reddit.post_reddit_recruitment"
                        updated = True
                    if updated:  # Save/log only if adjustments happened.
                        reddit_post_task.save()
                        logger.info("✅ Updated 'BB reddit evejobs post' periodic task")

                reddit_reply_schedule, _ = CrontabSchedule.objects.get_or_create(
                    minute="0",
                    hour="*",
                    day_of_week="*",
                    day_of_month="*",
                    month_of_year="*",
                    timezone="UTC",
                )

                reddit_reply_task, created_reddit_reply = PeriodicTask.objects.get_or_create(
                    name="BB reddit reply watcher",
                    defaults={
                        "crontab": reddit_reply_schedule,
                        "task": "aa_bb.tasks_reddit.monitor_reddit_replies",
                        "enabled": False,
                    },
                )
                if not created_reddit_reply:  # Task already existed; resync internals if needed.
                    updated = False
                    if reddit_reply_task.crontab != reddit_reply_schedule:  # Ensure cron schedule matches settings.
                        reddit_reply_task.crontab = reddit_reply_schedule
                        updated = True
                    if reddit_reply_task.task != "aa_bb.tasks_reddit.monitor_reddit_replies":  # Keep task path current.
                        reddit_reply_task.task = "aa_bb.tasks_reddit.monitor_reddit_replies"
                        updated = True
                    if updated:  # Save/log only when the record changed.
                        reddit_reply_task.save()
                        logger.info("✅ Updated 'BB reddit reply watcher' periodic task")
            else:
                deleted, _ = PeriodicTask.objects.filter(name__in=reddit_task_names).delete()
                if deleted:  # Inform ops when stale reddit tasks were purged.
                    logger.info("🗑️ Removed reddit periodic tasks because corp gate is not satisfied")
        except (OperationalError, ProgrammingError) as e:
            logger.warning(f"Could not register periodic task yet: {e}")
