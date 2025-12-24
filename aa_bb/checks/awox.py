"""
Fetches and caches "awox" killmails (friendly fire) for a user's characters.

The functions here encapsulate the networking against zKillboard/ESI,
cache management, and rendering helpers so the calling views do not have to
care about throttling or HTML generation.
"""

import time
from functools import lru_cache

import requests
from allianceauth.authentication.models import CharacterOwnership
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from esi.exceptions import HTTPNotModified
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..app_settings import (
    DATASOURCE,
    esi_tenant_kwargs,
    get_contact_email,
    get_owner_name,
    get_site_url,
    resolve_alliance_name,
    resolve_character_name,
    send_status_embed,
)

from ..esi_client import call_result, esi
from ..models import AwoxKillsCache, BigBrotherConfig

from allianceauth.services.hooks import get_extension_logger
logger = get_extension_logger(__name__)

USER_AGENT = f"{get_site_url()} Maintainer: {get_owner_name()} {get_contact_email()}"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip",
    "Accept": "application/json",
}

# How long we consider the cached awox data "fresh"
AWOX_CACHE_TTL_SECONDS = 60 * 60  # 60 minutes

# How many recent zKill entries we will hydrate per character (prevents runaway ESI calls)
MAX_KILLS_PER_CHARACTER = 25

# Limit zKill "down" notifications to once every 2 hours
_last_zkill_down_notice_monotonic = 0.0


@lru_cache(maxsize=512)
def _get_corp_name(corp_id):
    if not corp_id:
        return "None"
    try:
        operation = esi.client.Corporation.GetCorporationsCorporationId(corporation_id=corp_id)
        result, _ = call_result(operation)
        return result.get("name", f"Unknown ({corp_id})")
    except Exception:
        return f"Unknown ({corp_id})"


@lru_cache(maxsize=512)
def _get_alliance_name(alliance_id):
    if not alliance_id:
        return None
    try:
        return resolve_alliance_name(alliance_id)
    except Exception:
        return None


def _notify_zkill_down_once(preview: str, status: int | None, content_type: str | None):
    global _last_zkill_down_notice_monotonic
    now = time.monotonic()
    if now - _last_zkill_down_notice_monotonic < 2 * 60 * 60:
        return
    _last_zkill_down_notice_monotonic = now
    lines = [
        "zKillboard appears unavailable and awox checks will not work (non-JSON response).",
        f"status={status} content_type='{content_type}'",
        f"body preview: ```{preview}```"
    ]
    try:
        awox_notify = BigBrotherConfig.get_solo().awox_notify
        if awox_notify:
            send_status_embed(
                subject="zKillboard Unavailable",
                lines=lines,
                color=0xFF0000,
            )
    except Exception as e:
        logger.warning(f"Failed to send zKill down notification: {e}")


def _get_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_awox_kills(user_id, delay=0.2):
    """
    Return a deduplicated list of awox kill summaries for the given user.

    A DB cache keeps checklist reloads cheap; otherwise each character's
    recent awox activity is pulled from zKill, the full mail is hydrated
    via ESI, and the resulting summary is cached for future calls.
    """
    now = timezone.now()

    # DB cache with TTL: return cached kills if present & fresh
    try:
        cache = AwoxKillsCache.objects.get(pk=user_id)
        if cache.updated and (now - cache.updated).total_seconds() < AWOX_CACHE_TTL_SECONDS:
            try:
                cache.last_accessed = now
                cache.save(update_fields=["last_accessed"])
            except Exception:
                pass
            return cache.data or None
    except AwoxKillsCache.DoesNotExist:
        cache = None
    except Exception:
        cache = None

    characters = CharacterOwnership.objects.filter(user__id=user_id).select_related("character")
    char_id_to_name = {
        c.character.character_id: c.character.character_name
        for c in characters if getattr(c, "character", None)
    }
    char_ids = set(char_id_to_name.keys())
    if not char_ids:
        return None

    kills_by_id = {}
    session = _get_requests_session()

    for char_id in char_id_to_name.keys():
        zkill_url = f"https://zkillboard.com/api/characterID/{char_id}/awox/1/"
        start_ts = time.monotonic()
        try:
            response = session.get(zkill_url, timeout=(3, 10))
            response.raise_for_status()
        finally:
            elapsed = time.monotonic() - start_ts
            logger.info(
                "[AWOX][zKill] char_id=%s elapsed=%.3fs status=%s",
                char_id,
                elapsed,
                getattr(response, "status_code", "ERR"),
            )

        content_type = response.headers.get("Content-Type", "")
        text_lower = (response.text or "").lower()
        text_preview = (response.text or "").strip()[:200]

        if (
            not content_type.startswith("application/json")
            or "so a big oops happened" in text_lower
            or "cdn-cgi/challenge-platform" in text_lower
        ):
            logger.warning(
                "Non-JSON response from zKillboard for %s: status=%s content_type=%s body='%s'",
                char_id,
                response.status_code,
                content_type,
                text_preview,
            )
            _notify_zkill_down_once(text_preview, response.status_code, content_type)
            continue

        try:
            killmails = response.json()
        except ValueError as e:
            logger.warning(
                "Failed to decode zKillboard JSON for %s: %s. Body preview='%s'",
                char_id,
                e,
                text_preview,
            )
            _notify_zkill_down_once(text_preview, response.status_code, content_type)
            continue

        if isinstance(killmails, list):
            killmails = killmails[:MAX_KILLS_PER_CHARACTER]
        else:
            continue

        for kill in killmails:
            try:
                kill_id = int(kill.get("killmail_id", 0))
            except (ValueError, TypeError):
                continue

            hash_ = kill.get("zkb", {}).get("hash")
            value = kill.get("zkb", {}).get("totalValue", 0)

            if not kill_id or not hash_:
                continue

            # Defensive check against duplicates during the fetch loop
            if kill_id in kills_by_id:
                continue

            operation = esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
                killmail_id=kill_id,
                killmail_hash=hash_,
                **esi_tenant_kwargs(DATASOURCE),
            )

            try:
                full_kill, _ = call_result(operation)
            except HTTPNotModified:
                continue
            except Exception as e:
                logger.warning(f"Error fetching killmail from ESI for kill_id={kill_id}: {e}")
                continue

            victim = full_kill.get("victim", {})
            victim_id = victim.get("character_id")

            attackers = full_kill.get("attackers", []) or []
            involved_user_char_names = []

            if victim_id in char_ids:
                involved_user_char_names.append(char_id_to_name[victim_id])

            for a in attackers:
                a_id = a.get("character_id")
                if a_id and a_id in char_ids:
                    name = char_id_to_name[a_id]
                    if name not in involved_user_char_names:
                        involved_user_char_names.append(name)

            if not involved_user_char_names:
                continue

            # Resolve names for display
            vic_name = resolve_character_name(victim_id) if victim_id else "Unknown"

            # Find final blow attacker for display
            final_blow_attacker = next((a for a in attackers if a.get("final_blow")), attackers[0] if attackers else {})
            att_id = final_blow_attacker.get("character_id")
            att_name = resolve_character_name(att_id) if att_id else "Unknown"

            att_corp = _get_corp_name(final_blow_attacker.get("corporation_id"))
            att_alli = _get_alliance_name(final_blow_attacker.get("alliance_id"))

            vic_corp = _get_corp_name(victim.get("corporation_id"))
            vic_alli = _get_alliance_name(victim.get("alliance_id"))

            kills_by_id[kill_id] = {
                "value": int(value) if value is not None else 0,
                "link": f"https://zkillboard.com/kill/{kill_id}/",
                "chars": involved_user_char_names,
                "att_name": att_name,
                "att_corp": att_corp,
                "att_alli": att_alli,
                "vic_name": vic_name,
                "vic_corp": vic_corp,
                "vic_alli": vic_alli,
                "date": full_kill.get("killmail_time"),
            }

        time.sleep(delay)

    # Final deduplication by link just in case
    data_list = []
    seen_links = set()
    if kills_by_id:
        # Sort by killmail_id descending to generally show most recent first
        sorted_keys = sorted(kills_by_id.keys(), reverse=True)
        for k_id in sorted_keys:
            kill_data = kills_by_id[k_id]
            if kill_data['link'] not in seen_links:
                data_list.append(kill_data)
                seen_links.add(kill_data['link'])

    try:
        AwoxKillsCache.objects.update_or_create(
            user_id=user_id,
            defaults={"data": data_list, "last_accessed": now},
        )
    except Exception:
        try:
            if cache:
                cache.data = data_list
                cache.last_accessed = now
                cache.save()
        except Exception:
            pass

    return data_list if data_list else None

def render_awox_kills_html(userID):
    """
    Render the cached awox data into a simple Bootstrap friendly table.

    Returning `None` allows callers to skip rendering the section entirely
    when the user has no awox history (better UX than a blank table).
    """
    kills = fetch_awox_kills(userID)
    if not kills:  # Nothing to render, let callers hide the section entirely.
        return None

    html = '<table class="table table-striped table-hover stats">'
    html += '<thead><tr><th>Date</th><th>Character(s)</th><th>Attacker</th><th>Victim</th><th>Value</th><th>Link</th></tr></thead><tbody>'

    for kill in kills:
        chars = ", ".join(sorted(kill.get("chars", [])))
        value = "{:,}".format(kill.get("value", 0))
        link = kill.get("link", "#")

        date_val = kill.get("date")
        if hasattr(date_val, "strftime"):
            date_str = date_val.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = str(date_val).replace("T", " ").replace("Z", "")

        att_name = kill.get("att_name", "Unknown")
        att_html = f"<b>{att_name}</b><br>{kill.get('att_corp', '')}"
        if kill.get("att_alli"):
            att_html += f"<br><small>({kill.get('att_alli')})</small>"

        vic_name = kill.get("vic_name", "Unknown")
        vic_html = f"<b>{vic_name}</b><br>{kill.get('vic_corp', '')}"
        if kill.get("vic_alli"):
            vic_html += f"<br><small>({kill.get('vic_alli')})</small>"

        row_html = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{} ISK</td><td><a href="{}" target="_blank">View</a></td></tr>'
        html += format_html(row_html, date_str, chars, mark_safe(att_html), mark_safe(vic_html), value, link)

    html += '</tbody></table>'
    return html

def get_awox_kill_links(user_id):
    """
    Convenience helper used by notification code to embed kill links
    without having to duplicate the fetch/cache logic.
    """
    kills = fetch_awox_kills(user_id)
    if not kills:  # No cached kills yet
        return []

    results = []
    for kill in kills:
        if "link" not in kill:
            continue

        date_val = kill.get("date")
        if hasattr(date_val, "strftime"):
            date_str = date_val.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = str(date_val).replace("T", " ").replace("Z", "")

        results.append({
            "link": kill["link"],
            "date": date_str,
            "value": "{:,}".format(kill.get("value", 0))
        })

    return results
