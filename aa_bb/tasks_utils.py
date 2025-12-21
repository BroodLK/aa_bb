import logging
from django.db import transaction, IntegrityError
from django_celery_beat.models import PeriodicTask, CrontabSchedule, IntervalSchedule

logger = logging.getLogger(__name__)

def format_task_name(name: str) -> str:
    """
    Standardize a task name with 'AA-BB: ' prefix and proper capitalization.
    """
    prefix = "AA-BB: "
    if name.startswith(prefix):
        raw_name = name[len(prefix):]
    else:
        raw_name = name

    def format_word(word):
        upper_word = word.upper()
        if upper_word in ["BB", "CB", "CT", "DB"]:
            return upper_word
        elif upper_word == "LOA":
            return "LoA"
        return word.capitalize()

    # Replace hyphens with spaces for better capitalization
    raw_name = raw_name.replace('-', ' ')
    formatted_words = [format_word(w) for w in raw_name.split()]
    return prefix + " ".join(formatted_words)

def setup_periodic_task(
    name: str,
    task_path: str,
    schedule,
    enabled: bool = False,
):
    """
    Create or update a periodic task consistently.
    Ensures naming scheme 'AA-BB: ' and proper capitalization.
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
    if not task:
        task = PeriodicTask(name=standardized_name)
        updated = True

    if task.task != task_path:
        task.task = task_path
        updated = True

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
