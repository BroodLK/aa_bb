from django.test import TestCase
from django_celery_beat.models import PeriodicTask, IntervalSchedule, CrontabSchedule
from aa_bb.tasks_utils import setup_periodic_task, format_task_name
from aa_bb.models import BigBrotherConfig

class TestTaskSetup(TestCase):
    def setUp(self):
        self.schedule, _ = IntervalSchedule.objects.get_or_create(
            every=1,
            period=IntervalSchedule.HOURS,
        )

    def test_format_task_name(self):
        self.assertEqual(format_task_name("BB run regular updates"), "BB: Run Regular Updates")
        self.assertEqual(format_task_name("CB run regular updates"), "CB: Run Regular Updates")
        self.assertEqual(format_task_name("AA-BB: BB Run Regular Updates"), "BB: Run Regular Updates")
        self.assertEqual(format_task_name("tickets run regular updates"), "BB: Tickets Run Regular Updates")
        self.assertEqual(format_task_name("BB sync contacts from aa-contacts"), "BB: Sync Contacts From AA Contacts")

    def test_setup_periodic_task_creation(self):
        task_name = "BB run regular updates"
        setup_periodic_task(
            name=task_name,
            task_path="aa_bb.tasks.BB_run_regular_updates",
            schedule=self.schedule,
            enabled=True
        )

        expected_name = format_task_name(task_name)
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertEqual(task.task, "aa_bb.tasks.BB_run_regular_updates")
        self.assertTrue(task.enabled)

    def test_setup_periodic_task_renaming(self):
        # Create a task with the old naming scheme
        old_name = "BB run regular updates"
        PeriodicTask.objects.create(
            name=old_name,
            task="aa_bb.tasks.BB_run_regular_updates",
            interval=self.schedule,
            enabled=True
        )

        # Now run setup_periodic_task which should rename it
        setup_periodic_task(
            name=old_name,
            task_path="aa_bb.tasks.BB_run_regular_updates",
            schedule=self.schedule,
            enabled=False
        )

        expected_name = format_task_name(old_name)

        # Old task should be gone (renamed)
        self.assertFalse(PeriodicTask.objects.filter(name=old_name).exists())

        # New task should exist with the standardized name
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertFalse(task.enabled) # Should be disabled as requested in setup call

    def test_persistent_lifecycle(self):
        # Even if enabled=False, the task should be created
        task_name = "Some Optional Task"
        setup_periodic_task(
            name=task_name,
            task_path="aa_bb.tasks.some_task",
            schedule=self.schedule,
            enabled=False
        )

        expected_name = format_task_name(task_name)
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertFalse(task.enabled)

        # Calling it again with enabled=True should update it
        setup_periodic_task(
            name=task_name,
            task_path="aa_bb.tasks.some_task",
            schedule=self.schedule,
            enabled=True
        )
        task.refresh_from_db()
        self.assertTrue(task.enabled)
