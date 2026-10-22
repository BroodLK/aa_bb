# Standard Library
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.test import TestCase

# AA BigBrother
from aa_bb.models import BigBrotherConfig, UserStatus
from aa_bb.tasks import BB_update_single_user


class TestHostileAssetsFlapping(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser_flapping")
        self.config = BigBrotherConfig.get_solo()
        self.config.is_active = True
        self.config.asset_notify = True
        self.config.new_user_notify = True
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
    @patch("aa_bb.tasks.check_asset_audits_complete")
    def test_incomplete_audit_does_not_clear_hostile_assets(
        self,
        mock_check_audits_complete,
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
        mock_get_user_cyno_info,
    ):
        mock_get_user_cyno_info.return_value = {}
        mock_get_multiple_user_skill_info.return_value = {}
        mock_determine_character_state.return_value = {}
        mock_get_awox_kill_links.return_value = []
        mock_get_hostile_clone_locations.return_value = {}
        mock_get_user_hostile_notifications.return_value = {}
        mock_get_user_hostile_contracts.return_value = {}
        mock_get_user_hostile_mails.return_value = {}
        mock_get_user_hostile_transactions.return_value = {}
        mock_get_pings.return_value = ""
        mock_get_character_id.return_value = 12345
        mock_get_char_age.return_value = 100

        # Existing hostile state in DB
        status = UserStatus.objects.create(
            user=self.user,
            baseline_initialized=True,
            has_hostile_assets=True,
            hostile_assets={
                "LXQ2-T": {
                    "owner": "Rote Works",
                    "region": "Etherium Reach",
                    "records": [
                        {
                            "char_name": "Test Char",
                            "location_name": "LXQ2-T - Freeport",
                            "ships": ["Helios"],
                        }
                    ],
                }
            },
        )

        # Asset scan returns empty (e.g. transient sync or token glitch)
        mock_get_hostile_asset_locations.return_value = {}
        # Audit completeness check says audits are incomplete
        mock_check_audits_complete.return_value = False

        # Run task
        BB_update_single_user(self.user.id, "Test Char")

        # Status must retain hostile asset state
        status.refresh_from_db()
        self.assertTrue(status.has_hostile_assets)
        self.assertIn("LXQ2-T", status.hostile_assets)

        # No clear notification should have been sent
        if mock_send_discord_notifications.delay.called:
            args = mock_send_discord_notifications.delay.call_args[0]
            # Ensure no "🟢" was sent in notification chunks
            for chunk in args[1]:
                chunk_str = "".join(chunk) if isinstance(chunk, list) else str(chunk)
                self.assertNotIn("Hostile Asset Status: 🟢", chunk_str)

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
    @patch("aa_bb.tasks.check_asset_audits_complete")
    def test_complete_audit_clears_hostile_assets(
        self,
        mock_check_audits_complete,
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
        mock_get_user_cyno_info,
    ):
        mock_get_user_cyno_info.return_value = {}
        mock_get_multiple_user_skill_info.return_value = {}
        mock_determine_character_state.return_value = {}
        mock_get_awox_kill_links.return_value = []
        mock_get_hostile_clone_locations.return_value = {}
        mock_get_user_hostile_notifications.return_value = {}
        mock_get_user_hostile_contracts.return_value = {}
        mock_get_user_hostile_mails.return_value = {}
        mock_get_user_hostile_transactions.return_value = {}
        mock_get_pings.return_value = ""
        mock_get_character_id.return_value = 12345
        mock_get_char_age.return_value = 100

        # Existing hostile state in DB
        status = UserStatus.objects.create(
            user=self.user,
            baseline_initialized=True,
            has_hostile_assets=True,
            hostile_assets={
                "LXQ2-T": {
                    "owner": "Rote Works",
                    "region": "Etherium Reach",
                    "records": [
                        {
                            "char_name": "Test Char",
                            "location_name": "LXQ2-T - Freeport",
                            "ships": ["Helios"],
                        }
                    ],
                }
            },
        )

        # Asset scan returns empty and audits are verified complete
        mock_get_hostile_asset_locations.return_value = {}
        mock_check_audits_complete.return_value = True

        # Run task
        BB_update_single_user(self.user.id, "Test Char")

        # Status must now be cleared
        status.refresh_from_db()
        self.assertFalse(status.has_hostile_assets)
        self.assertEqual(status.hostile_assets, {})
