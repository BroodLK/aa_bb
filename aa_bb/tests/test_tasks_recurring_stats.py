from django.test import TestCase
from django.contrib.auth.models import User
from allianceauth.authentication.models import State, UserProfile
from allianceauth.eveonline.models import EveCharacter
from aa_bb.models import BigBrotherConfig, RecurringStatsConfig
from aa_bb.tasks_other import BB_send_recurring_stats
from unittest.mock import patch, MagicMock

class TestTasksRecurringStats(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Create states
        cls.state_member, _ = State.objects.get_or_create(name="Member", defaults={"priority": 10})
        cls.state_guest, _ = State.objects.get_or_create(name="Guest", defaults={"priority": 5})
        cls.state_alumni, _ = State.objects.get_or_create(name="Alumni", defaults={"priority": 3})

        # Create users and profiles
        cls.user_member = User.objects.create_user(username="member_user")
        cls.profile_member = cls.user_member.profile
        cls.profile_member.state = cls.state_member
        cls.char_member = EveCharacter.objects.create(
            character_id=101,
            character_name="Member Char",
            corporation_id=201,
            corporation_name="Member Corp",
            alliance_id=301,
            alliance_name="Member Alliance"
        )
        cls.profile_member.main_character = cls.char_member
        cls.profile_member.save()
        UserProfile.objects.filter(pk=cls.profile_member.pk).update(state=cls.state_member)

        cls.user_guest = User.objects.create_user(username="guest_user")
        cls.profile_guest = cls.user_guest.profile
        cls.profile_guest.state = cls.state_guest
        cls.char_guest = EveCharacter.objects.create(
            character_id=102,
            character_name="Guest Char",
            corporation_id=202,
            corporation_name="Guest Corp",
            alliance_id=302,
            alliance_name="Guest Alliance"
        )
        cls.profile_guest.main_character = cls.char_guest
        cls.profile_guest.save()
        UserProfile.objects.filter(pk=cls.profile_guest.pk).update(state=cls.state_guest)

        # Setup configs
        cls.bb_config = BigBrotherConfig.get_solo()
        cls.bb_config.is_active = True
        cls.bb_config.stats_webhook = "https://discord.com/api/webhooks/test"
        cls.bb_config.save()

        cls.stats_config = RecurringStatsConfig.get_solo()
        cls.stats_config.enabled = True
        cls.stats_config.states.add(cls.state_member, cls.state_guest, cls.state_alumni)
        cls.stats_config.save()

    @patch("aa_bb.tasks_other.send_status_embed")
    def test_stats_with_member_filter(self, mock_send_embed):
        # When member_alliances is set to only Member Alliance
        self.bb_config.member_alliances = "301"
        self.bb_config.save()

        BB_send_recurring_stats()

        # Check the last snapshot
        stats_cfg = RecurringStatsConfig.get_solo()
        snapshot = stats_cfg.last_snapshot

        # BOTH users should now be counted because we removed the filter in the task
        self.assertEqual(snapshot["auth_total"], 2)
        self.assertEqual(snapshot["auth_by_state"][str(self.state_member.pk)], 1)
        self.assertEqual(snapshot["auth_by_state"][str(self.state_guest.pk)], 1)

    @patch("aa_bb.tasks_other.send_status_embed")
    def test_stats_without_member_filter(self, mock_send_embed):
        # When no filter is set
        self.bb_config.member_alliances = ""
        self.bb_config.member_corporations = ""
        self.bb_config.save()

        BB_send_recurring_stats()

        # Check the last snapshot
        stats_cfg = RecurringStatsConfig.get_solo()
        snapshot = stats_cfg.last_snapshot

        # Both users should be counted
        self.assertEqual(snapshot["auth_total"], 2)
        self.assertEqual(snapshot["auth_by_state"][str(self.state_member.pk)], 1)
        self.assertEqual(snapshot["auth_by_state"][str(self.state_guest.pk)], 1)
