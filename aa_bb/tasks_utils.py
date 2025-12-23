import logging
from django.db import transaction, IntegrityError
from django_celery_beat.models import PeriodicTask, CrontabSchedule, IntervalSchedule

logger = logging.getLogger(__name__)

def format_task_name(name: str) -> str:
    """
    Standardize a task name with proper prefix and capitalization.
    'BB: ' is used for BB tasks, 'CB: ' for CB tasks.
    """
    # 1. Strip any existing known prefixes
    prefixes = ["AA-BB: ", "BB: ", "CB: "]
    raw_name = name
    original_prefix = None
    for p in prefixes:
        if raw_name.startswith(p):
            original_prefix = p
            raw_name = raw_name[len(p):]
            break

    # 2. Determine the new prefix and strip "BB " or "CB " if it's there
    if raw_name.upper().startswith("BB "):
        actual_prefix = "BB: "
        raw_name = raw_name[3:]
    elif raw_name.upper().startswith("CB "):
        actual_prefix = "CB: "
        raw_name = raw_name[3:]
    elif original_prefix == "CB: ":
        actual_prefix = "CB: "
    elif raw_name.lower().startswith("tickets run"):
        actual_prefix = "BB: "
    else:
        actual_prefix = "BB: "

    def format_word(word):
        upper_word = word.upper()
        if upper_word in ["BB", "CB", "CT", "DB", "AA", "ESI", "EVE", "API"]:
            return upper_word
        elif upper_word == "LOA":
            return "LoA"
        return word.capitalize()

    # Replace hyphens with spaces for better capitalization
    raw_name = raw_name.replace('-', ' ')
    formatted_words = [format_word(w) for w in raw_name.split()]
    return actual_prefix + " ".join(formatted_words)


def setup_periodic_task(
    name: str,
    task_path: str,
    schedule,
    enabled: bool = False,
    update_schedule: bool = False,
):
    """
    Create or update a periodic task consistently.
    Ensures naming scheme 'BB: ' or 'CB: ' and proper capitalization.
    Renames existing tasks if necessary.
    Never deletes tasks.
    """
    standardized_name = format_task_name(name)

    # 2. Find existing task
    task = PeriodicTask.objects.filter(name=standardized_name).first()
    old_task = PeriodicTask.objects.filter(name=name).first() if standardized_name != name else None

    if task and old_task and task.pk != old_task.pk:
        # Both exist! Delete the old one to avoid duplicates.
        logger.info(f"Found both '{standardized_name}' and '{name}'. Deleting the old one.")
        old_task.delete()
    elif not task and old_task:
        # Only old one exists, rename it
        task = old_task
        logger.info(f"Renaming periodic task '{name}' to '{standardized_name}'")
        task.name = standardized_name

    # 3. Create or update
    updated = False
    is_new = False
    if not task:
        task = PeriodicTask(name=standardized_name)
        updated = True
        is_new = True

    if task.task != task_path:
        task.task = task_path
        updated = True

    if is_new or update_schedule:
        if isinstance(schedule, CrontabSchedule):
            if task.crontab != schedule:
                task.crontab = schedule
                task.interval = None
                updated = True
        elif isinstance(schedule, IntervalSchedule):
            if task.interval != schedule:
                task.interval = schedule
                task.crontab = None
                updated = True

    if task.enabled != enabled:
        task.enabled = enabled
        updated = True

    if updated:
        try:
            with transaction.atomic():
                task.save()
            logger.info(f"✅  [AA-BB] - [Tasks] - {'Created' if task.pk is None else 'Updated'} '{standardized_name}' periodic task (enabled={enabled})")
        except IntegrityError:
            # Handle race condition where another process might have created it
            logger.warning(f"IntegrityError while saving periodic task '{standardized_name}', fetching existing.")
            task = PeriodicTask.objects.get(name=standardized_name)
    else:
        logger.info(f"ℹ️  [AA-BB] - [Tasks] - '{standardized_name}' periodic task already exists and is up to date")

    return task


def sync_periodic_tasks():
    """
    Consolidated function to ensure all BigBrother periodic tasks exist and are properly configured.
    Respects feature toggles and syncs schedules from configuration.
    """
    from .models import BigBrotherConfig
    from django.apps import apps

    config = BigBrotherConfig.get_solo()
    master_active = config.is_active

    # --- Schedules ---

    # Standard update interval (dynamic based on stagger window)
    stagger = max(getattr(config, "update_stagger_seconds", 3600), 3600)
    interval, _ = IntervalSchedule.objects.get_or_create(
        every=stagger,
        period=IntervalSchedule.SECONDS,
    )

    # Hourly at :25 past
    hourly_25_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute='25',
        hour='*',
        day_of_week='*',
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )

    # Daily at 12:00 UTC
    daily_12_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute='0',
        hour='12',
        day_of_week='*',
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )

    # Weekly on Sunday at 12:00 UTC
    weekly_sun_12_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute='0',
        hour='12',
        day_of_week='0',
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )

    # Daily cleanup at 01:00 UTC
    cleanup_schedule, _ = CrontabSchedule.objects.get_or_create(
        minute='0',
        hour='1',
        day_of_week='*',
        day_of_month='*',
        month_of_year='*',
        timezone='UTC',
    )

    # --- Task List ---
    # format: name, task_path, schedule, active_attr, update_schedule, is_master_dependent
    # if schedule_attr is provided, it overrides schedule from config
    tasks_to_sync = [
        {
            "name": "BB run regular updates",
            "task_path": "aa_bb.tasks.BB_run_regular_updates",
            "schedule": interval,
            "active_attr": "is_active",
            "update_schedule": True,
        },
        {
            "name": "CB run regular updates",
            "task_path": "aa_bb.tasks_cb.CB_run_regular_updates",
            "schedule": hourly_25_schedule,
            "active_attr": "is_active",
            "update_schedule": True,
        },
        {
            "name": "BB kickstart stale CT modules",
            "task_path": "aa_bb.tasks_ct.kickstart_stale_ct_modules",
            "schedule": hourly_25_schedule,
            "active_attr": "is_active",
            "update_schedule": True,
        },
        {
            "name": "BB run regular DB cleanup",
            "task_path": "aa_bb.tasks_cb.BB_daily_DB_cleanup",
            "schedule": cleanup_schedule,
            "active_attr": "is_active",
            "update_schedule": True,
        },
        {
            "name": "tickets run regular updates",
            "task_path": "aa_bb.tasks_tickets.hourly_compliance_check",
            "schedule": hourly_25_schedule,
            "active_attr": "is_active", # We use is_active as the master switch for tickets too
            "update_schedule": True,
        },
        {
            "name": "BB run regular LoA updates",
            "task_path": "aa_bb.tasks_cb.BB_run_regular_loa_updates",
            "schedule": daily_12_schedule,
            "active_attr": "is_loa_active",
            "update_schedule": True,
        },
        {
            "name": "BB check member compliance",
            "task_path": "aa_bb.tasks_cb.check_member_compliance",
            "schedule": daily_12_schedule,
            "active_attr": "is_paps_active",
            "update_schedule": True,
        },
        {
            "name": "BB send recurring stats",
            "task_path": "aa_bb.tasks_other.BB_send_recurring_stats",
            "schedule_attr": "stats_schedule",
            "default_schedule": weekly_sun_12_schedule,
            "active_attr": "are_recurring_stats_active",
            "update_schedule": True,
        },
        {
            "name": "BB send daily message",
            "task_path": "aa_bb.tasks_cb.BB_send_daily_messages",
            "schedule_attr": "dailyschedule",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_daily_messages_active",
            "update_schedule": True,
        },
        {
            "name": "BB send optional message 1",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message1",
            "schedule_attr": "optschedule1",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_opt_messages1_active",
            "update_schedule": True,
        },
        {
            "name": "BB send optional message 2",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message2",
            "schedule_attr": "optschedule2",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_opt_messages2_active",
            "update_schedule": True,
        },
        {
            "name": "BB send optional message 3",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message3",
            "schedule_attr": "optschedule3",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_opt_messages3_active",
            "update_schedule": True,
        },
        {
            "name": "BB send optional message 4",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message4",
            "schedule_attr": "optschedule4",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_opt_messages4_active",
            "update_schedule": True,
        },
        {
            "name": "BB send optional message 5",
            "task_path": "aa_bb.tasks_cb.BB_send_opt_message5",
            "schedule_attr": "optschedule5",
            "default_schedule": daily_12_schedule,
            "active_attr": "are_opt_messages5_active",
            "update_schedule": True,
        },
    ]

    # Add optional tasks if dependencies are met
    if apps.is_installed("aa_contacts"):
        tasks_to_sync.append({
            "name": "BB sync contacts from aa-contacts",
            "task_path": "aa_bb.tasks.BB_sync_contacts_from_aa_contacts",
            "schedule": hourly_25_schedule,
            "active_attr": "auto_import_contacts_enabled",
            "update_schedule": True,
        })

    for task_info in tasks_to_sync:
        name = task_info["name"]
        task_path = task_info["task_path"]

        # Resolve schedule
        schedule_attr = task_info.get("schedule_attr")
        if schedule_attr:
            schedule = getattr(config, schedule_attr, None) or task_info.get("default_schedule") or daily_12_schedule
        else:
            schedule = task_info.get("schedule")

        # Resolve enabled status (master switch + feature toggle)
        feature_enabled = bool(getattr(config, task_info["active_attr"], False))
        enabled = master_active and feature_enabled

        # Special case: 'is_active' tasks ARE the master switch
        if task_info["active_attr"] == "is_active":
            enabled = master_active

        setup_periodic_task(
            name=name,
            task_path=task_path,
            schedule=schedule,
            enabled=enabled,
            update_schedule=task_info.get("update_schedule", False)
        )
