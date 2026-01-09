from django.test import TestCase
from unittest.mock import MagicMock, patch
from aa_bb.models import BigBrotherConfig
from aa_bb.checks.sus_trans import is_transaction_hostile, get_user_transactions
from aa_bb.checks_cb.sus_trans import is_transaction_hostile as is_transaction_hostile_cb, get_user_transactions as get_user_transactions_cb

class TestSusTrans(TestCase):
    def setUp(self):
        self.cfg = BigBrotherConfig.get_solo()
        self.cfg.show_market_transactions = True
        self.cfg.save()

    def test_major_hub_filtering_enabled(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": 30000142,
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 10,
            "second_party_corporation_id": 20,
        }

        # When major hubs are DISABLED (market_transactions_show_major_hubs=False)
        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.save()

        self.assertFalse(is_transaction_hostile(tx), "Transaction in major hub should be filtered out when major hubs are disabled")

    def test_major_hub_hostile_corp_show_market_enabled(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": 30000142,
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 666, # Hostile corp
            "second_party_corporation_id": 20,
        }

        self.cfg.hostile_corporations = "666"
        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.show_market_transactions = True
        self.cfg.save()

        self.assertFalse(is_transaction_hostile(tx), "Hostile market transaction in major hub should be filtered out when major hubs are disabled and show_market_transactions is True")

    def test_major_hub_hostile_corp_show_market_disabled(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": 30000142,
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 666, # Hostile corp
            "second_party_corporation_id": 20,
        }

        self.cfg.hostile_corporations = "666"
        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.show_market_transactions = False # HERE IS THE PROBLEM
        self.cfg.save()

        # This currently fails because hub filters are gated by show_market_transactions
        self.assertFalse(is_transaction_hostile(tx), "Hostile market transaction in major hub should be filtered out even when show_market_transactions is False if hubs are disabled")

    def test_string_system_id(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": "30000142", # STRING
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 10,
            "second_party_corporation_id": 20,
        }

        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.show_market_transactions = True
        self.cfg.save()

        # This currently fails because "30000142" in {30000142, ...} is False
        self.assertFalse(is_transaction_hostile(tx), "String system_id should still be filtered out by major hub filter")

    def test_major_hub_hostile_corp_show_market_disabled_cb(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": 30000142,
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 666, # Hostile corp
            "second_party_corporation_id": 20,
        }

        self.cfg.hostile_corporations = "666"
        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.show_market_transactions = False
        self.cfg.save()

        self.assertFalse(is_transaction_hostile_cb(tx), "Hostile market transaction in major hub should be filtered out (corp version)")

    def test_string_system_id_cb(self):
        # Jita system_id = 30000142
        tx = {
            "type": "market_transaction",
            "system_id": "30000142", # STRING
            "first_party_id": 1,
            "second_party_id": 2,
            "first_party_corporation_id": 10,
            "second_party_corporation_id": 20,
        }

        self.cfg.market_transactions_show_major_hubs = False
        self.cfg.show_market_transactions = True
        self.cfg.save()

        self.assertFalse(is_transaction_hostile_cb(tx), "String system_id should still be filtered out by major hub filter (corp version)")

    @patch('aa_bb.checks.sus_trans.is_hostile_unified')
    def test_transaction_location_hostility(self, mock_is_hostile):
        tx = {
            "type": "player_trading",
            "location_id": 1000000000001,
            "system_id": 30000001,
            "first_party_id": 1,
            "second_party_id": 2,
        }

        mock_is_hostile.return_value = True
        self.assertTrue(is_transaction_hostile(tx), "Transaction in hostile location should be hostile if it's a suspicious type")

        mock_is_hostile.return_value = False
        self.assertFalse(is_transaction_hostile(tx), "player_trading should NOT be hostile if parties are neutral")

        # Test with a hostile party
        mock_is_hostile.return_value = True
        self.assertTrue(is_transaction_hostile(tx), "Transaction with hostile party should be hostile")

        # Test a normal transaction that is ONLY hostile due to location (should be False now)
        mock_is_hostile.return_value = True
        tx["first_party_corporation_id"] = 100 # Safe
        tx["type"] = "normal_transaction"
        self.assertFalse(is_transaction_hostile(tx), "Normal transaction in hostile location should NOT be hostile if parties are safe/neutral")

    @patch('aa_bb.checks.sus_trans.resolve_location_name')
    @patch('aa_bb.checks.sus_trans.resolve_location_system_id')
    @patch('aa_bb.checks.sus_trans.get_entity_info')
    def test_structure_id_resolution(self, mock_get_entity_info, mock_resolve_system, mock_resolve_name):
        # Setup mock for get_entity_info
        mock_get_entity_info.return_value = {
            'name': 'Test Character',
            'corp_id': 123,
            'corp_name': 'Test Corp',
            'alli_id': 456,
            'alli_name': 'Test Alliance'
        }

        # Setup mock for location resolution
        mock_resolve_name.return_value = "Test Structure"
        mock_resolve_system.return_value = 30000142 # Jita

        # Create a mock entry
        entry = MagicMock()
        entry.entry_id = 1001
        entry.date = MagicMock()
        entry.first_party_id = 1
        entry.second_party_id = 2
        entry.context_id = 123456789
        entry.context_id_type = "structure_id"
        entry.amount = 1000
        entry.balance = 5000
        entry.description = "Test transaction"
        entry.reason = "None"
        entry.ref_type = "player_donation"

        qs = [entry]

        result = get_user_transactions(qs)

        tx = result[1001]
        self.assertEqual(tx["context"], "Structure: Test Structure")
        self.assertEqual(tx["system_id"], 30000142)

    @patch('aa_bb.checks_cb.sus_trans.resolve_location_name')
    @patch('aa_bb.checks_cb.sus_trans.resolve_location_system_id')
    @patch('aa_bb.checks_cb.sus_trans.get_entity_info')
    @patch('aa_bb.checks_cb.sus_trans.get_eve_entity_type')
    def test_structure_id_resolution_cb(self, mock_get_type, mock_get_entity_info, mock_resolve_system, mock_resolve_name):
        # Setup mock for get_entity_info
        mock_get_entity_info.return_value = {
            'name': 'Test Character',
            'corp_id': 123,
            'corp_name': 'Test Corp',
            'alli_id': 456,
            'alli_name': 'Test Alliance'
        }
        mock_get_type.return_value = "character"

        # Setup mock for location resolution
        mock_resolve_name.return_value = "Test Structure CB"
        mock_resolve_system.return_value = 30000142 # Jita

        # Create a mock entry
        entry = MagicMock()
        entry.entry_id = 2001
        entry.date = MagicMock()
        entry.first_party_id = 1
        entry.second_party_id = 2
        entry.context_id = 987654321
        entry.context_id_type = "structure_id"
        entry.amount = 2000
        entry.balance = 6000
        entry.description = "Test transaction corp"
        entry.reason = "None"
        entry.ref_type = "player_donation"

        qs = [entry]

        result = get_user_transactions_cb(qs)

        tx = result[2001]
        self.assertEqual(tx["context"], "Structure: Test Structure CB")
        self.assertEqual(tx["system_id"], 30000142)
