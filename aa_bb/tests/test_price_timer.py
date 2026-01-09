from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from unittest.mock import patch, MagicMock
from aa_bb.models import BigBrotherConfig, EveItemPrice
from aa_bb.app_settings import get_or_create_prices

class TestPriceTimer(TestCase):
    def setUp(self):
        self.config = BigBrotherConfig.get_solo()
        self.config.market_transactions_price_max_age = 10
        self.config.save()

    @patch('aa_bb.checks.sus_trans.requests.get')
    def test_price_timer_configurable(self, mock_get):
        # Create a price object that is 8 days old
        # With default 7 days it should refresh
        # With our config 10 days it should NOT refresh
        item_id = 1234
        old_date = timezone.now() - timedelta(days=8)
        price_obj = EveItemPrice.objects.create(
            eve_type_id=item_id,
            buy=100.0,
            sell=110.0
        )
        # Manually update the 'updated' field because it's auto_now=True
        EveItemPrice.objects.filter(eve_type_id=item_id).update(updated=old_date)

        # Reload from DB to be sure
        price_obj.refresh_from_db()

        # Call get_or_create_prices
        result = get_or_create_prices(item_id)

        # Verify it did NOT refresh (mock_get not called)
        self.assertFalse(mock_get.called)
        self.assertEqual(result.buy, 100.0)

        # Now change config to 5 days
        self.config.market_transactions_price_max_age = 5
        self.config.save()

        # Setup mock for refresh
        mock_response = MagicMock()
        mock_response.status_code = 200
        # Fuzzwork format
        mock_response.json.return_value = {
            "1234": {
                "buy": {"max": 200.0, "percentile": 190.0},
                "sell": {"min": 210.0, "percentile": 220.0}
            }
        }
        mock_get.return_value = mock_response
        self.config.market_transactions_price_method = 'Fuzzwork'
        self.config.save()

        # Call get_or_create_prices again
        result = get_or_create_prices(item_id)

        # Verify it DID refresh
        self.assertTrue(mock_get.called)
        self.assertEqual(result.buy, 200.0) # market_transactions_price_instant is True by default
