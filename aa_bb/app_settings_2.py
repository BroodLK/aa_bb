"""
Supplemental helpers for BigBrother: corp/alliance info caching, DLC toggles,
webhook utilities, and deployment helpers that were split out of app_settings.
"""

from django.apps import apps
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
from .models import Alliance_names, BigBrotherConfig
import re
import os
import time
from collections import deque
import subprocess
import sys
import requests
from httpx import RequestError
from esi.exceptions import HTTPClientError, HTTPServerError, HTTPNotModified
from .esi_client import esi, to_plain, call_result, parse_expires
from .esi_cache import expiry_cache_key, get_cached_expiry, set_cached_expiry


from allianceauth.services.hooks import get_extension_logger
logger = get_extension_logger(__name__)

VERBOSE_WEBHOOK_LOGGING = True

TTL_SHORT = timedelta(hours=4)

def get_owner_name():
    """Return the character name used to sign API requests / dashboards."""
    from allianceauth.eveonline.models import EveCharacter
    try:
        char = EveCharacter.objects.filter(character_ownership__user__is_superuser=True).first()
        if char:  # Prefer the first superuser's main pilot name.
            return char.character_name
    except Exception:
        pass
    return None  # Fallback

def get_alliance_name(alliance_id):
    """Resolve an alliance id to its name with DB/ESI caching."""
    if not alliance_id:  # Allow callers to pass None when corp not in alliance.
        return "None"
    # Try DB cache first with 4h TTL
    try:
        rec = Alliance_names.objects.get(pk=alliance_id)
    except Alliance_names.DoesNotExist:
        rec = None

    expiry_key = expiry_cache_key("alliance_name", alliance_id)
    expiry_hint = get_cached_expiry(expiry_key)
    if rec:  # Return cached names when TTL has not expired.
        now_ts = timezone.now()
        if expiry_hint and expiry_hint > now_ts:  # Redis TTL still valid.
            return rec.name
        if expiry_hint is None and now_ts - rec.updated < TTL_SHORT:  # DB TTL still valid.
            return rec.name

    cached_name = rec.name if rec else None
    operation = esi.client.Alliance.GetAlliancesAllianceId(
        alliance_id=alliance_id
    )
    try:
        result, expires_at = call_result(operation)
        set_cached_expiry(expiry_key, expires_at)
        name = result.get("name", f"Unknown ({alliance_id})")
    except HTTPNotModified as exc:
        set_cached_expiry(expiry_key, parse_expires(getattr(exc, "headers", {})))
        if cached_name:  # Use stale DB name when ESI returned 304.
            name = cached_name
        else:
            try:
                result, expires_at = call_result(operation, use_etag=False)
                set_cached_expiry(expiry_key, expires_at)
                name = result.get("name", f"Unknown ({alliance_id})")
            except Exception as e:
                logger.warning(f"Error fetching alliance {alliance_id} after 304: {e}")
                name = f"Unknown ({alliance_id})"
    except (HTTPClientError, HTTPServerError) as e:
        logger.warning(f"ESI error fetching alliance {alliance_id}: {e}")
        name = f"Unknown ({alliance_id})"
    except (RequestError, requests.exceptions.RequestException) as e:
        logger.warning(f"Network error fetching alliance {alliance_id}: {e}")
        name = f"Unknown ({alliance_id})"

    try:
        Alliance_names.objects.update_or_create(pk=alliance_id, defaults={"name": name})
    except Exception:
        pass

    return name

def get_site_url():  # regex sso url
    """Derive the site root from the configured SSO callback URL."""
    regex = r"^(.+)\/s.+"
    matches = re.finditer(regex, settings.ESI_SSO_CALLBACK_URL, re.MULTILINE)
    url = "http://"

    for m in matches:
        url = m.groups()[0]  # first match

    return url

def get_contact_email():  # regex sso url
    """Contact email published to CCP via ESI user agent metadata."""
    return settings.ESI_USER_CONTACT_EMAIL


def aablacklist_active():
    """Return True when the optional AllianceAuth blacklist app is installed."""
    return apps.is_installed("blacklist")


def afat_active():
    """Return True when the AFAT plugin is loaded in this deployment."""
    return apps.is_installed("afat")


_webhook_history = deque()  # stores timestamp floats of last webhook sends
_channel_history = deque()  # stores timestamp floats of last channel sends


def send_message(message, hook: str = None):
    """
    Sends `message` via Discord webhook with rate limiting.

    `message` may be:
      - str  -> sent as {"content": message}, with chunking.
      - dict -> sent directly as JSON, for embeds etc.
    """
    webhook_url = hook or BigBrotherConfig.get_solo().webhook

    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug(
            "[WEBHOOK] send_message called | type=%s | hook_override=%s",
            type(message).__name__,
            bool(hook),
        )

    MAX_LEN = 2000
    SPLIT_LEN = 1900

    def _throttle():
        now = time.monotonic()

        if VERBOSE_WEBHOOK_LOGGING:
            logger.debug(
                "[WEBHOOK] throttle check | webhook_hist=%d | channel_hist=%d",
                len(_webhook_history),
                len(_channel_history),
            )

        # -- webhook limit: max 5 per 2s --
        while len(_webhook_history) >= 5:
            earliest = _webhook_history[0]
            elapsed = now - earliest
            if elapsed >= 2.0:
                popped = _webhook_history.popleft()
                if VERBOSE_WEBHOOK_LOGGING:
                    logger.debug(
                        "[WEBHOOK] throttle: popped webhook ts %.4f", popped
                    )
            else:
                sleep_for = 2.0 - elapsed
                if VERBOSE_WEBHOOK_LOGGING:
                    logger.debug(
                        "[WEBHOOK] throttle: webhook sleep %.3fs", sleep_for
                    )
                time.sleep(sleep_for)
                now = time.monotonic()

        # -- channel limit: max 30 per 60s --
        while len(_channel_history) >= 30:
            earliest = _channel_history[0]
            elapsed = now - earliest
            if elapsed >= 60.0:
                popped = _channel_history.popleft()
                if VERBOSE_WEBHOOK_LOGGING:
                    logger.debug(
                        "[WEBHOOK] throttle: popped channel ts %.4f", popped
                    )
            else:
                sleep_for = 60.0 - elapsed
                if VERBOSE_WEBHOOK_LOGGING:
                    logger.debug(
                        "[WEBHOOK] throttle: channel sleep %.3fs", sleep_for
                    )
                time.sleep(sleep_for)
                now = time.monotonic()

        _webhook_history.append(now)
        _channel_history.append(now)

        if VERBOSE_WEBHOOK_LOGGING:
            logger.debug(
                "[WEBHOOK] throttle pass | new_ts=%.4f", now
            )

    def _post_with_retries(payload: dict):
        attempt = 0
        while True:
            attempt += 1
            _throttle()

            if VERBOSE_WEBHOOK_LOGGING:
                logger.debug(
                    "[WEBHOOK] POST attempt %d | keys=%s",
                    attempt,
                    list(payload.keys()),
                )

            try:
                response = requests.post(webhook_url, json=payload)

                if VERBOSE_WEBHOOK_LOGGING:
                    logger.debug(
                        "[WEBHOOK] HTTP %s | len=%d",
                        response.status_code,
                        len(response.content or b""),
                    )

                response.raise_for_status()
                return

            except requests.exceptions.HTTPError:
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        backoff = float(retry_after)
                    except (TypeError, ValueError):
                        backoff = 1.0

                    logger.warning(
                        "[WEBHOOK] 429 rate limit | retry_after=%.3f",
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                else:
                    logger.error(
                        "[WEBHOOK] HTTP error %s: %s",
                        response.status_code,
                        response.text,
                    )
                    return

            except Exception as e:
                logger.error(
                    "[WEBHOOK] Exception sending payload | attempt=%d | err=%r",
                    attempt,
                    e,
                )
                time.sleep(2.0)
                continue

    # ---- DISPATCH ----

    if isinstance(message, dict):
        if VERBOSE_WEBHOOK_LOGGING:
            logger.debug(
                "[WEBHOOK] sending embed payload | embeds=%d",
                len(message.get("embeds", [])),
            )
        _post_with_retries(message)
        return

    # message is str
    if VERBOSE_WEBHOOK_LOGGING:
        logger.debug(
            "[WEBHOOK] sending text | length=%d",
            len(message),
        )

    if len(message) <= MAX_LEN:
        _post_with_retries({"content": message})
        return

    # Chunking path
    logger.info(
        "[WEBHOOK] chunking long message | length=%d",
        len(message),
    )

    raw_lines = message.split("\n")
    parts = []

    for line in raw_lines:
        if len(line) <= MAX_LEN:
            parts.append(line)
        else:
            logger.debug(
                "[WEBHOOK] splitting overlong line | length=%d",
                len(line),
            )
            for i in range(0, len(line), SPLIT_LEN):
                prefix = "# split due to length\n" if i > 0 else ""
                parts.append(prefix + line[i : i + SPLIT_LEN])

    buffer = ""
    for part in parts:
        candidate = buffer + ("\n" if buffer else "") + part
        if len(candidate) > MAX_LEN:
            logger.debug(
                "[WEBHOOK] flushing chunk | length=%d",
                len(buffer),
            )
            _post_with_retries({"content": buffer})
            buffer = part
        else:
            buffer = candidate

    if buffer:
        logger.debug(
            "[WEBHOOK] flushing final chunk | length=%d",
            len(buffer),
        )
        _post_with_retries({"content": buffer})
