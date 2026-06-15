# Standard Library
from unittest.mock import Mock, patch

# Third Party
from django_celery_beat.models import IntervalSchedule, PeriodicTask

# Django
from django.test import TestCase

# AA BigBrother
from aa_bb.tasks_utils import format_task_name, setup_periodic_task, sync_periodic_tasks


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
            name=task_name, task_path="aa_bb.tasks.BB_run_regular_updates", schedule=self.schedule, enabled=True
        )

        expected_name = format_task_name(task_name)
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertEqual(task.task, "aa_bb.tasks.BB_run_regular_updates")
        self.assertTrue(task.enabled)

    def test_setup_periodic_task_renaming(self):
        # Create a task with the old naming scheme
        old_name = "BB run regular updates"
        PeriodicTask.objects.create(
            name=old_name, task="aa_bb.tasks.BB_run_regular_updates", interval=self.schedule, enabled=True
        )

        # Now run setup_periodic_task which should rename it
        setup_periodic_task(
            name=old_name, task_path="aa_bb.tasks.BB_run_regular_updates", schedule=self.schedule, enabled=False
        )

        expected_name = format_task_name(old_name)

        # Old task should be gone (renamed)
        self.assertFalse(PeriodicTask.objects.filter(name=old_name).exists())

        # New task should exist with the standardized name
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertFalse(task.enabled)  # Should be disabled as requested in setup call

    def test_persistent_lifecycle(self):
        # Even if enabled=False, the task should be created
        task_name = "Some Optional Task"
        setup_periodic_task(name=task_name, task_path="aa_bb.tasks.some_task", schedule=self.schedule, enabled=False)

        expected_name = format_task_name(task_name)
        task = PeriodicTask.objects.get(name=expected_name)
        self.assertFalse(task.enabled)

        # Calling it again with enabled=True should update it
        setup_periodic_task(name=task_name, task_path="aa_bb.tasks.some_task", schedule=self.schedule, enabled=True)
        task.refresh_from_db()
        self.assertTrue(task.enabled)

    @patch("aa_bb.task_helpers.periodic_tasks.setup_periodic_task")
    @patch("aa_bb.task_helpers.periodic_tasks.PeriodicTask.objects.filter")
    @patch("aa_bb.task_helpers.periodic_tasks.CrontabSchedule.objects.get_or_create")
    @patch("aa_bb.task_helpers.periodic_tasks.IntervalSchedule.objects.get_or_create")
    @patch("django.apps.apps.is_installed", return_value=False)
    @patch("aa_bb.models.BigBrotherConfig.get_solo")
    def test_sync_periodic_tasks_uses_parent_package_models_import(
        self,
        mock_get_solo,
        _mock_is_installed,
        mock_interval_get_or_create,
        mock_crontab_get_or_create,
        mock_task_filter,
        mock_setup_periodic_task,
    ):
        config = Mock(
            is_active=True,
            update_stagger_seconds=3600,
            is_loa_active=False,
            is_paps_active=False,
            are_recurring_stats_active=False,
            are_daily_messages_active=False,
            are_opt_messages1_active=False,
            are_opt_messages2_active=False,
            are_opt_messages3_active=False,
            are_opt_messages4_active=False,
            are_opt_messages5_active=False,
            stats_schedule=None,
            dailyschedule=None,
            optschedule1=None,
            optschedule2=None,
            optschedule3=None,
            optschedule4=None,
            optschedule5=None,
        )
        config.refresh_from_db = Mock()
        mock_get_solo.return_value = config
        mock_interval_get_or_create.return_value = (Mock(spec=IntervalSchedule), False)
        mock_crontab_get_or_create.return_value = (Mock(), False)
        mock_task_filter.return_value.first.return_value = None

        sync_periodic_tasks()

        mock_get_solo.assert_called_once()
        config.refresh_from_db.assert_called_once()
        self.assertTrue(mock_setup_periodic_task.called)
