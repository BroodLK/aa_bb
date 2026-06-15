# Standard Library
from unittest.mock import Mock, patch

# Django
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.checks.awox import fetch_awox_kills


class TestAwoxDetector(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="awox-user")

    def _create_owned_character(self, character_id, character_name):
        character = EveCharacter.objects.create(
            character_id=character_id,
            character_name=character_name,
            corporation_id=1001,
            corporation_name="Test Corp",
        )
        CharacterOwnership.objects.create(
            character=character,
            user=self.user,
            owner_hash=f"hash-{character_id}",
        )
        return character

    def _build_session(self, payload):
        response = Mock()
        response.raise_for_status.return_value = None
        response.headers = {"Content-Type": "application/json"}
        response.json.return_value = payload

        session = Mock()
        session.get.return_value = response
        session.close.return_value = None
        return session

    @patch("aa_bb.checks.awox.time.sleep", return_value=None)
    @patch("aa_bb.checks.awox._try_acquire_zkill_slot", return_value=True)
    @patch("aa_bb.checks.awox.ESIHandler.get_killmails_killmail_id_killmail_hash")
    @patch("aa_bb.checks.awox._get_requests_session")
    def test_skips_same_user_alt_on_alt_kill(
        self,
        mock_get_requests_session,
        mock_get_killmail,
        _mock_rate_limit,
        _mock_sleep,
    ):
        victim = self._create_owned_character(900001, "Victim Alt")
        attacker = self._create_owned_character(900002, "Attacker Alt")

        mock_get_requests_session.return_value = self._build_session(
            [{"killmail_id": 12345, "zkb": {"hash": "hash-12345", "totalValue": 2500000}}]
        )
        mock_get_killmail.return_value = {
            "killmail_time": timezone.now(),
            "victim": {
                "character_id": victim.character_id,
                "corporation_id": victim.corporation_id,
            },
            "attackers": [
                {
                    "character_id": attacker.character_id,
                    "corporation_id": attacker.corporation_id,
                    "final_blow": True,
                }
            ],
        }

        kills = fetch_awox_kills(self.user.id, force_refresh=True)

        self.assertEqual(kills, [])

    @patch("aa_bb.checks.awox.time.sleep", return_value=None)
    @patch("aa_bb.checks.awox._try_acquire_zkill_slot", return_value=True)
    @patch("aa_bb.checks.awox.ESIHandler.get_killmails_killmail_id_killmail_hash")
    @patch("aa_bb.checks.awox._get_requests_session")
    def test_skips_same_character_self_kill(
        self,
        mock_get_requests_session,
        mock_get_killmail,
        _mock_rate_limit,
        _mock_sleep,
    ):
        character = self._create_owned_character(900003, "Self Killer")

        mock_get_requests_session.return_value = self._build_session(
            [{"killmail_id": 22345, "zkb": {"hash": "hash-22345", "totalValue": 1250000}}]
        )
        mock_get_killmail.return_value = {
            "killmail_time": timezone.now(),
            "victim": {
                "character_id": character.character_id,
                "corporation_id": character.corporation_id,
            },
            "attackers": [
                {
                    "character_id": character.character_id,
                    "corporation_id": character.corporation_id,
                    "final_blow": True,
                }
            ],
        }

        kills = fetch_awox_kills(self.user.id, force_refresh=True)

        self.assertEqual(kills, [])

    @patch("aa_bb.checks.awox.time.sleep", return_value=None)
    @patch("aa_bb.checks.awox._try_acquire_zkill_slot", return_value=True)
    @patch("aa_bb.checks.awox.ESIHandler.get_killmails_killmail_id_killmail_hash")
    @patch("aa_bb.checks.awox._get_requests_session")
    def test_keeps_external_victim_awox(
        self,
        mock_get_requests_session,
        mock_get_killmail,
        _mock_rate_limit,
        _mock_sleep,
    ):
        attacker = self._create_owned_character(900004, "Real Attacker")
        victim = EveCharacter.objects.create(
            character_id=900005,
            character_name="External Victim",
            corporation_id=1002,
            corporation_name="Other Corp",
        )

        mock_get_requests_session.return_value = self._build_session(
            [{"killmail_id": 32345, "zkb": {"hash": "hash-32345", "totalValue": 9500000}}]
        )
        mock_get_killmail.return_value = {
            "killmail_time": timezone.now(),
            "victim": {
                "character_id": victim.character_id,
                "corporation_id": victim.corporation_id,
            },
            "attackers": [
                {
                    "character_id": attacker.character_id,
                    "corporation_id": attacker.corporation_id,
                    "final_blow": True,
                }
            ],
        }

        kills = fetch_awox_kills(self.user.id, force_refresh=True)

        self.assertEqual(len(kills), 1)
        self.assertTrue(kills[0]["is_attacker"])
        self.assertEqual(kills[0]["chars"], [attacker.character_name])
        self.assertEqual(kills[0]["vic_name"], victim.character_name)
