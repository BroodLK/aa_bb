from django.test import TestCase
from django.contrib.auth.models import User
from unittest.mock import patch, MagicMock
from aa_bb.models import UserStatus, BigBrotherConfig
from aa_bb.tasks import BB_update_single_user

class TestTasksNotifications(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser")
        self.config = BigBrotherConfig.get_solo()
        self.config.is_active = True
        self.config.new_user_notify = False
        self.config.save()

    @patch("aa_bb.tasks.get_user_cyno_info")
    @patch("aa_bb.tasks.get_multiple_user_skill_info")
    @patch("aa_bb.tasks.determine_character_state")
    @patch("aa_bb.tasks.get_awox_kill_links")
    @patch("aa_bb.tasks.get_hostile_clone_locations")
    @patch("aa_bb.tasks.get_hostile_asset_locations")
    @patch("aa_bb.tasks.get_user_hostile_notifications")
    @patch("aa_bb.tasks.get_user_hostile_contracts")
    @patch("aa_bb.tasks.get_user_hostile_mails")
    @patch("aa_bb.tasks.get_user_hostile_transactions")
    @patch("aa_bb.tasks.BB_send_discord_notifications")
    @patch("aa_bb.tasks.get_character_id")
    @patch("aa_bb.tasks.get_char_age")
    @patch("aa_bb.tasks.get_pings")
    def test_no_notification_on_first_run_when_disabled(
        self,
        mock_get_pings,
        mock_get_char_age,
        mock_get_character_id,
        mock_send_discord_notifications,
        mock_get_user_hostile_transactions,
        mock_get_user_hostile_mails,
        mock_get_user_hostile_contracts,
        mock_get_user_hostile_notifications,
        mock_get_hostile_asset_locations,
        mock_get_hostile_clone_locations,
        mock_get_awox_kill_links,
        mock_determine_character_state,
        mock_get_multiple_user_skill_info,
        mock_get_user_cyno_info
    ):
        # Setup mocks to return some "suspicious" data to trigger changes
        mock_get_user_cyno_info.return_value = {}
        mock_get_multiple_user_skill_info.return_value = {"Test Char": {"total_sp": 1000000}}
        mock_determine_character_state.return_value = {}
        mock_get_awox_kill_links.return_value = []
        mock_get_hostile_clone_locations.return_value = {}
        mock_get_hostile_asset_locations.return_value = {}
        mock_get_user_hostile_notifications.return_value = {"1": "Some hostile contact"}
        mock_get_user_hostile_contracts.return_value = {}
        mock_get_user_hostile_mails.return_value = {}
        mock_get_user_hostile_transactions.return_value = {}
        mock_get_pings.return_value = ""
        mock_get_character_id.return_value = 12345
        mock_get_char_age.return_value = 100

        # Run the task
        BB_update_single_user(self.user.id, "Test Char")

        # Verify that BB_send_discord_notifications.delay was NOT called
        # because new_user_notify is False and it's the first run (baseline_initialized=False)
        self.assertFalse(mock_send_discord_notifications.delay.called)

        # Verify baseline_initialized is now True
        status = UserStatus.objects.get(user=self.user)
        self.assertTrue(status.baseline_initialized)
