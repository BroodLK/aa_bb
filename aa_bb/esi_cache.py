"""
Helpers for caching ESI expiry timestamps in Django's cache backend.
"""

from datetime import datetime, timezone as dt_timezone

from django.core.cache import cache
from django.utils import timezone


def expiry_cache_key(kind: str, identifier) -> str:
    """Generate a namespaced cache key used to store expiry hints."""
    return f"aa_bb:esi_expiry:{kind}:{identifier}"


def get_cached_expiry(key: str) -> datetime | None:
    """Fetch previously stored expiry timestamps and convert them back to datetimes."""
    ts = cache.get(key)
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=dt_timezone.utc)
    except (TypeError, ValueError):
        cache.delete(key)
        return None


def set_cached_expiry(key: str, expires_at: datetime | None) -> None:
    """
    Write a future expiry timestamp (or clear the cache when None).

    The epoch value is stored to avoid timezone serialization issues.
    """
    if not expires_at:
        cache.delete(key)
        return
    now = timezone.now()
    timeout = max(1, int((expires_at - now).total_seconds()))
    cache.set(key, expires_at.timestamp(), timeout)
