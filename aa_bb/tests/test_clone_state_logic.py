from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from allianceauth.eveonline.models import EveCharacter
from allianceauth.authentication.models import CharacterOwnership
from aa_bb.models import BigBrotherConfig
from aa_bb.checks.clone_state import determine_character_state

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

    @patch('aa_bb.checks.clone_state.CharacterAudit')
    @patch('aa_bb.checks.clone_state.Skill')
    @patch('aa_bb.checks.clone_state.get_user_characters')
    def test_omega_detection_from_omega_only_skill(self, mock_get_chars, mock_skill, mock_audit):
        """
        Verify that a character with an active Omega-only skill is detected as Omega.
        Skill 21803 (Capital Repair Systems) is in skills.json but not in alpha_skills.json.
        """
        mock_get_chars.return_value = {1001: "Test Character"}

        # Mocking fallback_skill_ids to include 21803
        with patch('aa_bb.checks.clone_state._load_fallback_skill_ids', return_value=[21803]):
            # Simulate the character has Capital Repair Systems at level 1 active
            # This skill is NOT in alpha_skills.json
            mock_skill.objects.filter.return_value.values.return_value = [
                {
                    "character__character__character_id": 1001,
                    "skill_id": 21803,
                    "trained_skill_level": 1,
                    "active_skill_level": 1,
                }
            ]

            # For the first call in determine_character_state (fetching skills in alpha_skill_ids | extra_skill_ids)
            # 21803 is not in alpha_skill_ids, and we haven't specified it as extra_skill_id.
            # So the first query (line 148) might return empty if 21803 is not in alpha_skill_ids.
            # But the fallback query (line 210) will fetch it.

            # To be safe, let's make the filter for line 148 return empty
            # and the filter for line 211 return our skill row.

            mock_skill_filter = mock_skill.objects.filter
            def side_effect(*args, **kwargs):
                if 'skill_id__in' in kwargs:
                    # Check if it's the fallback query
                    if 21803 in kwargs['skill_id__in'] and len(kwargs['skill_id__in']) == 1:
                        return MagicMock(values=MagicMock(return_value=[
                            {
                                "skill_id": 21803,
                                "trained_skill_level": 1,
                                "active_skill_level": 1,
                            }
                        ]))
                return MagicMock(values=MagicMock(return_value=[]))

            mock_skill_filter.side_effect = side_effect

            result = determine_character_state(self.user.id)

            self.assertEqual(result[1001]["state"], "omega", "Character with active Omega-only skill should be Omega")
            self.assertEqual(result[1001]["skill_used"], 21803)
