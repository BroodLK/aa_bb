"""
Fetches and caches "awox" killmails (friendly fire) for a user's characters.

The functions here encapsulate the networking against zKillboard/ESI,
cache management, and rendering helpers so the calling views do not have to
care about throttling or HTML generation.
"""

import html
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


def fetch_awox_kills(user_id, delay=0.2, force_refresh=False):
    """
    Return a deduplicated list of awox kill summaries for the given user.

    A DB cache keeps checklist reloads cheap; otherwise each character's
    recent awox activity is pulled from zKill, the full mail is hydrated
    via ESI, and the resulting summary is cached for future calls.
    """
    now = timezone.now()
    logger.info(f"[AWOX] Fetching kills for user_id={user_id} (force_refresh={force_refresh})")

    # DB cache with TTL: return cached kills if present & fresh
    cache_obj = None
    existing_data = []
    try:
        cache_obj = AwoxKillsCache.objects.get(pk=user_id)
        existing_data = cache_obj.data or []
        if not force_refresh and cache_obj.updated and (now - cache_obj.updated).total_seconds() < AWOX_CACHE_TTL_SECONDS:
            logger.info(f"[AWOX] Cache HIT for user_id={user_id} ({len(existing_data)} kills)")
            try:
                cache_obj.last_accessed = now
                cache_obj.save(update_fields=["last_accessed"])
            except Exception:
                pass
            return existing_data
    except AwoxKillsCache.DoesNotExist:
        logger.info(f"[AWOX] Cache MISS for user_id={user_id}")
    except Exception as e:
        logger.warning(f"[AWOX] Error accessing AwoxKillsCache for user {user_id}: {e}")

    characters = CharacterOwnership.objects.filter(user__id=user_id).select_related("character")
    char_id_to_name = {
        c.character.character_id: c.character.character_name
        for c in characters if getattr(c, "character", None)
    }
    char_ids = set(char_id_to_name.keys())
    if not char_ids:
        logger.warning(f"[AWOX] No characters found for user_id={user_id}")
        return existing_data

    # Use a dict keyed by killmail_id to deduplicate across characters and merge with existing
    kills_by_id = {}
    for entry in existing_data:
        try:
            # Extract ID from link: https://zkillboard.com/kill/123456/
            link = entry.get('link', '')
            if 'kill/' in link:
                k_id = int(link.split('kill/')[1].split('/')[0])
                kills_by_id[k_id] = entry
        except (ValueError, IndexError, TypeError):
            continue

    session = _get_requests_session()
    new_kills_found = 0
    fetch_error_occurred = False

    try:
        for char_id in char_id_to_name.keys():
            try:
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
                    fetch_error_occurred = True
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
                    fetch_error_occurred = True
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

                    # Defensive check against duplicates during the fetch loop or if already in cache
                    if kill_id in kills_by_id:
                        continue

                    hash_ = kill.get("zkb", {}).get("hash")
                    value = kill.get("zkb", {}).get("totalValue", 0)

                    if not kill_id or not hash_:
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
                    victim_is_user = victim_id in char_ids

                    attackers = full_kill.get("attackers", []) or []
                    attacker_ids_user = [
                        a.get("character_id") for a in attackers
                        if a.get("character_id") and a.get("character_id") in char_ids
                    ]

                    involved_user_char_names = []
                    if victim_is_user:
                        involved_user_char_names.append(char_id_to_name[victim_id])

                    for a_id in attacker_ids_user:
                        name = char_id_to_name[a_id]
                        if name not in involved_user_char_names:
                            involved_user_char_names.append(name)

                    if not involved_user_char_names:
                        continue

                    is_attacker = len(attacker_ids_user) > 0

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
                        "is_attacker": is_attacker,
                        "att_name": att_name,
                        "att_corp": att_corp,
                        "att_alli": att_alli,
                        "vic_name": vic_name,
                        "vic_corp": vic_corp,
                        "vic_alli": vic_alli,
                        "date": full_kill.get("killmail_time"),
                    }
                    new_kills_found += 1

                time.sleep(delay)
            except Exception as e:
                logger.warning(f"Failed to fetch AWOX kills for character {char_id} of user {user_id}: {e}")
                fetch_error_occurred = True
                continue

        # Final deduplication by link just in case, and sort
        data_list = []
        seen_links = set()
        if kills_by_id:
            # Sort by killmail_id descending to show most recent first
            sorted_keys = sorted(kills_by_id.keys(), reverse=True)
            # Keep a reasonable limit in the cache
            for k_id in sorted_keys[:100]:
                kill_data = kills_by_id[k_id]
                if kill_data['link'] not in seen_links:
                    data_list.append(kill_data)
                    seen_links.add(kill_data['link'])

        # Always update the cache if we didn't have a catastrophic error
        # even if no new kills found (to update the 'updated' timestamp)
        try:
            AwoxKillsCache.objects.update_or_create(
                user_id=user_id,
                defaults={"data": data_list, "last_accessed": now},
            )
            logger.info(f"Updated AwoxKillsCache for user {user_id} with {len(data_list)} kills (new={new_kills_found})")
        except Exception as e:
            logger.error(f"Failed to save AwoxKillsCache for user {user_id}: {e}")

        return data_list

    except Exception as e:
        logger.exception(f"catastrophic error in fetch_awox_kills for user {user_id}: {e}")
        return data_list if 'data_list' in locals() and data_list else existing_data

    finally:
        # CRITICAL: Close session to prevent memory leak
        session.close()
        import gc
        gc.collect()

def render_awox_kills_html(userID):
    """
    Render the cached awox data into a simple Bootstrap friendly table.

    Returning the standardized empty message allows for consistent UI.
    """
    kills = fetch_awox_kills(userID)
    if not kills:  # Nothing to render, return standardized empty table.
        return '<table class="table stats"><tbody><tr><td class="text-center">No recent AWOX kills found.</td></tr></tbody></table>'

    html = '<table class="table table-striped table-hover stats">'
    html += '<thead><tr><th>Date</th><th>Character(s)</th><th>Attacker</th><th>Victim</th><th>Value</th><th>Link</th></tr></thead><tbody>'

    for kill in kills:
        chars_list = sorted(kill.get("chars", []))
        if kill.get("is_attacker", False):
            chars = mark_safe(f'<span class="text-danger">{html.escape(", ".join(chars_list))}</span>')
        else:
            chars = ", ".join(chars_list)
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

        row_html = '<tr class="text-danger"><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{} ISK</td><td><a href="{}" target="_blank">View</a></td></tr>'
        html += format_html(row_html, date_str, chars, mark_safe(att_html), mark_safe(vic_html), value, link)

    html += '</tbody></table>'
    return html

def get_awox_kill_links(user_id, force_refresh=False):
    """
    Convenience helper used by notification code to embed kill links
    without having to duplicate the fetch/cache logic.
    """
    kills = fetch_awox_kills(user_id, force_refresh=force_refresh)
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
            "value": "{:,}".format(kill.get("value", 0)),
            "is_attacker": kill.get("is_attacker", False)
        })

    return results
