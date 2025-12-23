from django.test import TestCase
from aa_bb.models import BigBrotherConfig
from aa_bb.checks.sus_trans import is_transaction_hostile
from aa_bb.checks_cb.sus_trans import is_transaction_hostile as is_transaction_hostile_cb

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
