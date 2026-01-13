import time
import json
import os
from unittest.mock import patch, MagicMock
from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from allianceauth.authentication.models import CharacterOwnership
from aa_bb.models import BigBrotherConfig, UserStatus, EveItemPrice
from aa_bb.checks.clone_state import determine_character_state
from aa_bb.checks.sus_trans import get_user_hostile_transactions

class PerformanceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser")
        cls.main_char = EveCharacter.objects.create(
            character_id=1001,
            character_name="Main Character",
            corporation_id=2001,
            corporation_name="Test Corp",
        )
        # Mock ownership
        CharacterOwnership.objects.create(character=cls.main_char, user=cls.user, owner_hash="abc")

    def test_clone_state_performance(self):
        """Test performance of determine_character_state with simulated skills."""
        # Setup mocks for corptools models and config
        with patch('aa_bb.checks.clone_state.CharacterAudit') as mock_audit, \
             patch('aa_bb.checks.clone_state.Skill') as mock_skill, \
             patch('aa_bb.checks.clone_state.get_user_characters') as mock_get_chars, \
             patch('aa_bb.checks.clone_state.BigBrotherConfig.get_solo') as mock_get_solo:

            mock_config = MagicMock()
            mock_config.update_cache_ttl_hours = 24
            mock_config.update_maintenance_window_start = timezone.now().time()
            mock_config.update_maintenance_window_end = (timezone.now() + timezone.timedelta(hours=1)).time()
            mock_get_solo.return_value = mock_config

            mock_get_chars.return_value = {1001: "Main Character"}

            # Simulate 50 skills
            mock_skill.objects.filter.return_value.values.return_value = [
                {
                    "character__character__character_id": 1001,
                    "skill_id": 3400 + i,
                    "trained_skill_level": 5,
                    "active_skill_level": 5,
                } for i in range(50)
            ]

            mock_audit.objects.filter.return_value.select_related.return_value.only.return_value = []

            start_time = time.time()
            for _ in range(100): # Run 100 times to get a better average
                determine_character_state(self.user.id)
            end_time = time.time()

            avg_time = (end_time - start_time) / 100
            print(f"\n[PERFORMANCE] determine_character_state avg time: {avg_time:.6f}s")

    def test_transaction_threshold_performance(self):
        """Test performance of transaction filtering and threshold checking."""
        # Setup mock transactions
        mock_txs = {}
        for i in range(100):
            mock_txs[i] = {
                "entry_id": i,
                "type_id": 34, # Tritanium
                "raw_amount": 1000000.0,
                "quantity": 100000,
                "system_id": 30000142, # Jita
                "first_party_id": 1001,
                "second_party_id": 9001,
                "first_party_corporation_id": 2001,
                "second_party_corporation_id": 3001,
                "type": "player_trading",
            }

        with patch('aa_bb.checks.sus_trans.get_user_characters') as mock_get_chars, \
             patch('aa_bb.checks.sus_trans.get_user_transactions') as mock_get_txs, \
             patch('aa_bb.checks.sus_trans.is_transaction_hostile') as mock_hostile, \
             patch('aa_bb.app_settings.is_above_market_threshold') as mock_threshold, \
             patch('aa_bb.checks.sus_trans.BigBrotherConfig.get_solo') as mock_get_solo:

            mock_config = MagicMock()
            mock_config.show_market_transactions = True
            mock_get_solo.return_value = mock_config

            mock_get_chars.return_value = {1001: "Main Character"}
            mock_get_txs.return_value = mock_txs
            mock_hostile.return_value = True
            mock_threshold.return_value = True

            start_time = time.time()
            for _ in range(10): # Run 10 times
                get_user_hostile_transactions(self.user.id)
            end_time = time.time()

            avg_time = (end_time - start_time) / 10
            print(f"[PERFORMANCE] get_user_hostile_transactions (100 tx) avg time: {avg_time:.6f}s")

    def test_is_above_threshold_logic_performance(self):
        """Test the performance of the price threshold logic itself."""
        from aa_bb.app_settings import is_above_market_threshold

        tx = {
            "type_id": 34,
            "raw_amount": 5.0, # Way above market
            "quantity": 1,
        }

        # Mocking external price fetches
        with patch('aa_bb.app_settings.EVEUNIVERSE_INSTALLED', False), \
             patch('aa_bb.app_settings.get_or_create_prices') as mock_prices:

            mock_price_obj = MagicMock()
            mock_price_obj.buy = 1.0
            mock_price_obj.sell = 1.2
            mock_prices.return_value = mock_price_obj

            start_time = time.time()
            for _ in range(1000):
                is_above_market_threshold(tx["type_id"], tx["raw_amount"], 10.0)
            end_time = time.time()

            avg_time = (end_time - start_time) / 1000
            print(f"[PERFORMANCE] is_above_threshold logic avg time: {avg_time:.6f}s")
