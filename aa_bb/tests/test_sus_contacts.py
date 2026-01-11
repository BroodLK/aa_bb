from django.test import TestCase
from unittest.mock import patch, MagicMock
from aa_bb.checks.sus_contacts import get_user_hostile_notifications
from aa_bb.models import BigBrotherConfig

class TestSusContacts(TestCase):
    def setUp(self):
        self.cfg = BigBrotherConfig.get_solo()
        self.cfg.is_active = True
        self.cfg.save()

    @patch('aa_bb.checks.sus_contacts.get_user_contacts')
    @patch('aa_bb.checks.sus_contacts.get_hostile_state')
    def test_hostile_notification_with_positive_standing(self, mock_hostile_state, mock_get_user_contacts):
        # Setup: Positive standing with a hostile contact
        mock_get_user_contacts.return_value = {
            123: {
                'contact_type': 'character',
                'character': 'Hostile Guy',
                'standing': 5.0,
                'characters': {'Main Char'},
                'coid': 456,
                'corporation': 'Hostile Corp',
                'aid': 789,
                'alliance': 'Hostile Alliance',
            }
        }
        mock_hostile_state.side_effect = lambda cid, ctype: True

        notifications = get_user_hostile_notifications(1)

        self.assertEqual(len(notifications), 1)
        self.assertIn("is on hostile list", notifications[123])

    @patch('aa_bb.checks.sus_contacts.get_user_contacts')
    @patch('aa_bb.checks.sus_contacts.get_hostile_state')
    def test_no_hostile_notification_with_negative_standing(self, mock_hostile_state, mock_get_user_contacts):
        # Setup: Negative standing with a hostile contact
        mock_get_user_contacts.return_value = {
            123: {
                'contact_type': 'character',
                'character': 'Hostile Guy',
                'standing': -10.0,
                'characters': {'Main Char'},
                'coid': 456,
                'corporation': 'Hostile Corp',
                'aid': 789,
                'alliance': 'Hostile Alliance',
            }
        }
        mock_hostile_state.side_effect = lambda cid, ctype: True

        notifications = get_user_hostile_notifications(1)

        # Currently this will FAIL because it alerts regardless of standing
        self.assertEqual(len(notifications), 0, "Should not alert on negative standing with a hostile entity as it is redundant")

    @patch('aa_bb.checks.sus_contacts.get_user_contacts')
    @patch('aa_bb.checks.sus_contacts.get_hostile_state')
    def test_hostile_notification_with_neutral_standing(self, mock_hostile_state, mock_get_user_contacts):
        # Setup: Neutral standing (0) with a hostile contact
        mock_get_user_contacts.return_value = {
            123: {
                'contact_type': 'character',
                'character': 'Hostile Guy',
                'standing': 0.0,
                'characters': {'Main Char'},
                'coid': 456,
                'corporation': 'Hostile Corp',
                'aid': 789,
                'alliance': 'Hostile Alliance',
            }
        }
        mock_hostile_state.side_effect = lambda cid, ctype: True

        notifications = get_user_hostile_notifications(1)

        self.assertEqual(len(notifications), 1)
        self.assertIn("is on hostile list", notifications[123])
