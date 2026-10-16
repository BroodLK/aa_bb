from datetime import datetime, timezone as dt_timezone

from django.test import TestCase

from aa_bb.esi_cache import expiry_cache_key, get_cached_expiry, set_cached_expiry
from aa_bb.esi_client import parse_expires


class TestEsiExpiryHelpers(TestCase):
    def test_parse_expires_returns_utc_aware_datetime(self):
        expires_at = parse_expires({"Expires": "Wed, 21 Oct 2015 07:28:00 GMT"})

        self.assertEqual(
            expires_at,
            datetime(2015, 10, 21, 7, 28, tzinfo=dt_timezone.utc),
        )

    def test_cached_expiry_round_trips_as_utc(self):
        key = expiry_cache_key("corp", 42)
        expires_at = datetime(2030, 1, 1, 12, 0, tzinfo=dt_timezone.utc)

        set_cached_expiry(key, expires_at)

        self.assertEqual(get_cached_expiry(key), expires_at)
