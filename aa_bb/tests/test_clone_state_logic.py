# Standard Library
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.test import TestCase

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.checks.clone_state import determine_character_state
from aa_bb.models import BigBrotherConfig


class CloneStateLogicTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser")
        cls.char = EveCharacter.objects.create(
            character_id=1001,
            character_name="Test Character",
            corporation_id=2001,
            corporation_name="Test Corp",
        )
        CharacterOwnership.objects.create(character=cls.char, user=cls.user, owner_hash="abc")

        # Ensure config exists
        BigBrotherConfig.get_solo()

    @patch("aa_bb.checks.clone_state.CharacterAudit")
    @patch("aa_bb.checks.clone_state.Skill")
    @patch("aa_bb.checks.clone_state.get_user_characters")
    def test_omega_detection_from_omega_only_skill(self, mock_get_chars, mock_skill, mock_audit):
        """
        Verify that a character with an active Omega-only skill is detected as Omega.
        Skill 21803 (Capital Repair Systems) is in skills.json but not in alpha_skills.json.
        """
        mock_get_chars.return_value = {1001: "Test Character"}

        # Mocking fallback_skill_ids to include 21803
        with patch("aa_bb.checks.clone_state._load_fallback_skill_ids", return_value=[21803]):
            # Current implementation bulk-fetches alpha and fallback skills in one query.
            mock_skill.objects.filter.return_value.values.return_value = [
                {
                    "character__character__character_id": 1001,
                    "skill_id": 21803,
                    "trained_skill_level": 1,
                    "active_skill_level": 1,
                }
            ]

            result = determine_character_state(self.user.id)

            self.assertEqual(result[1001]["state"], "omega", "Character with active Omega-only skill should be Omega")
            self.assertEqual(result[1001]["skill_used"], 21803)
