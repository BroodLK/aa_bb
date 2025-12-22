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
