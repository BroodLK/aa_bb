"""
Fetches and caches "awox" killmails (friendly fire) for a user's characters.

The functions here encapsulate the networking against zKillboard/ESI,
cache management, and rendering helpers so the calling views do not have to
care about throttling or HTML generation.
"""

# Standard Library
import html
import time
from collections import deque
from datetime import timedelta
from functools import lru_cache
from threading import Lock

# Third Party
import requests
from requests.adapters import HTTPAdapter

# Django
from django.core.cache import cache
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from ..app_settings import (
    DATASOURCE,
    _serialize_datetime,
    esi_tenant_kwargs,
    get_contact_email,
    get_owner_name,
    get_site_url,
    resolve_alliance_name,
    resolve_character_name,
    resolve_corporation_name,
    send_status_embed,
)
from ..models import AwoxKillsCache, BigBrotherConfig
from ..providers import ESIHandler

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
MAX_KILLS_LIMIT = 100

# Hard cap to stay well under zKill's 10 req/s limit across the process.
ZKILL_MAX_REQUESTS_PER_SECOND = 5
_ZKILL_RATE_WINDOW_SECONDS = 1.0
_zkill_request_times = deque()
_zkill_rate_lock = Lock()

# Limit zKill "down" notifications to once every 2 hours
_last_zkill_down_notice_monotonic = 0.0


@lru_cache(maxsize=512)
def _get_corp_name(corp_id):
    if not corp_id:
        return "None"
    try:
        return resolve_corporation_name(corp_id)
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
        f"body preview: ```{preview}```",
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


def _try_acquire_zkill_global_slot() -> bool:
    """
    Cross-process rate limiter via Django cache.

    Falls back to allow if cache is unavailable.
    """
    try:
        bucket = int(time.time() // _ZKILL_RATE_WINDOW_SECONDS)
        cache_key = f"aa_bb:zkill_rate:{bucket}"
        if cache.add(cache_key, 1, timeout=int(_ZKILL_RATE_WINDOW_SECONDS * 2)):
            return True
        count = cache.incr(cache_key)
        return count <= ZKILL_MAX_REQUESTS_PER_SECOND
    except Exception:
        return True


def _try_acquire_zkill_slot() -> bool:
    now = time.monotonic()
    with _zkill_rate_lock:
        cutoff = now - _ZKILL_RATE_WINDOW_SECONDS
        while _zkill_request_times and _zkill_request_times[0] <= cutoff:
            _zkill_request_times.popleft()
        if len(_zkill_request_times) >= ZKILL_MAX_REQUESTS_PER_SECOND:
            return False
        _zkill_request_times.append(now)
        return _try_acquire_zkill_global_slot()


def _parse_cached_kill_date(value):
    if hasattr(value, "strftime"):
        return value
    if not value:
        return None
    try:
        # Django
        from django.utils.dateparse import parse_datetime

        return parse_datetime(value)
    except Exception:
        return None


def _merge_cached_kills(existing_data, new_data, cutoff):
    merged = {}
    for entry in existing_data or []:
        if not isinstance(entry, dict):
            continue
        link = entry.get("link")
        if not link:
            continue
        dt = _parse_cached_kill_date(entry.get("date"))
        if not dt or dt < cutoff:
            continue
        cached_entry = dict(entry)
        cached_entry["date"] = dt
        merged[link] = cached_entry

    for entry in new_data:
        link = entry.get("link")
        if not link:
            continue
        merged[link] = entry

    merged_list = list(merged.values())
    merged_list.sort(key=lambda x: x["date"], reverse=True)
    return merged_list


def _get_requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    # Disable automatic retries so the global rate limiter governs every request.
    adapter = HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_awox_kills(user_id, delay=0.2, force_refresh=False):
    """
    Fetch AWOX kills from zKillboard for all characters owned by the user
    within the past 24 hours.
    """
    # Alliance Auth
    from allianceauth.eveonline.models import EveCharacter

    now = timezone.now()
    cutoff = now - timedelta(days=365)
    existing_data = []

    try:
        cache_obj = AwoxKillsCache.objects.get(user_id=user_id)
        if not force_refresh and (now - cache_obj.updated).total_seconds() < AWOX_CACHE_TTL_SECONDS:
            # Filter cached data by date before returning
            filtered_cached = []
            for kill in cache_obj.data:
                dt = kill.get("date")
                if not dt:
                    continue
                if not hasattr(dt, "strftime"):
                    # likely a string from JSON cache
                    try:
                        # Django
                        from django.utils.dateparse import parse_datetime

                        dt = parse_datetime(dt)
                    except Exception:
                        continue
                if dt and dt >= cutoff:
                    filtered_cached.append(kill)
            return filtered_cached
        existing_data = cache_obj.data
    except AwoxKillsCache.DoesNotExist:
        pass

    characters = CharacterOwnership.objects.filter(user__id=user_id).select_related("character")
    char_id_to_name = {
        c.character.character_id: c.character.character_name for c in characters if getattr(c, "character", None)
    }
    char_ids = set(char_id_to_name.keys())

    if not char_ids:
        return []

    session = _get_requests_session()
    processed_kills = {}  # kill_id -> data
    skipped_char_ids = []

    try:
        for char_id in char_ids:
            # Use the awox endpoint which only returns friendly-fire kills
            zkill_url = f"https://zkillboard.com/api/characterID/{char_id}/awox/1/"
            try:
                if not _try_acquire_zkill_slot():
                    skipped_char_ids.append(char_id)
                    continue
                response = session.get(zkill_url, timeout=(5, 15))
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("application/json"):
                    _notify_zkill_down_once(response.text[:200], response.status_code, content_type)
                    continue

                data = response.json()
                if not isinstance(data, list):
                    continue

                for k in data[:MAX_KILLS_LIMIT]:
                    kill_id = k.get("killmail_id")
                    if not kill_id or kill_id in processed_kills:
                        continue

                    try:
                        # Hydrate from ESI
                        hash_ = k.get("zkb", {}).get("hash")
                        if not hash_:
                            continue

                        full_kill = ESIHandler.get_killmails_killmail_id_killmail_hash(
                            killmail_id=kill_id,
                            killmail_hash=hash_,
                            operation_kwargs=esi_tenant_kwargs(DATASOURCE),
                            allow_not_modified=True,
                            swallow_errors=True,
                        )
                        if full_kill is None:
                            full_kill = ESIHandler.get_killmails_killmail_id_killmail_hash(
                                killmail_id=kill_id,
                                killmail_hash=hash_,
                                operation_kwargs=esi_tenant_kwargs(DATASOURCE),
                                use_etag=False,
                                swallow_errors=True,
                            )
                        if not full_kill:
                            continue

                        victim = full_kill.get("victim", {})
                        victim_id = victim.get("character_id")
                        attackers = full_kill.get("attackers", []) or []

                        # Who from our user is involved?
                        involved_user_char_ids = []
                        if victim_id in char_ids:
                            involved_user_char_ids.append(victim_id)

                        for a in attackers:
                            aid = a.get("character_id")
                            if aid in char_ids and aid not in involved_user_char_ids:
                                involved_user_char_ids.append(aid)

                        if not involved_user_char_ids:
                            continue  # Should not happen if zKill filter worked correctly for this char

                        is_attacker = any(a.get("character_id") in char_ids for a in attackers)

                        # Resolve names
                        if victim_id:
                            try:
                                vic_name = EveCharacter.objects.get(character_id=victim_id).character_name
                            except (EveCharacter.DoesNotExist, AttributeError):
                                vic_name = resolve_character_name(victim_id) or "Unknown"
                        else:
                            vic_name = "Unknown"

                        fb_attacker = next(
                            (a for a in attackers if a.get("final_blow")), attackers[0] if attackers else {}
                        )
                        att_id = fb_attacker.get("character_id")
                        if att_id:
                            try:
                                att_name = EveCharacter.objects.get(character_id=att_id).character_name
                            except (EveCharacter.DoesNotExist, AttributeError):
                                att_name = resolve_character_name(att_id) or "Unknown"
                        else:
                            att_name = "Unknown"

                        processed_kills[kill_id] = {
                            "value": int(k.get("zkb", {}).get("totalValue", 0)),
                            "link": f"https://zkillboard.com/kill/{kill_id}/",
                            "chars": [char_id_to_name[cid] for cid in involved_user_char_ids],
                            "is_attacker": is_attacker,
                            "att_name": att_name,
                            "att_corp": _get_corp_name(fb_attacker.get("corporation_id")),
                            "att_alli": _get_alliance_name(fb_attacker.get("alliance_id")),
                            "vic_name": vic_name,
                            "vic_corp": _get_corp_name(victim.get("corporation_id")),
                            "vic_alli": _get_alliance_name(victim.get("alliance_id")),
                            "date": full_kill.get("killmail_time"),
                        }
                        time.sleep(delay)
                    except Exception as ke:
                        logger.warning(f"[AWOX] Error processing kill {kill_id}: {ke}")
                        continue

            except Exception as e:
                logger.warning(f"[AWOX] Failed fetch for char {char_id}: {e}")

        # Final result list
        cutoff = now - timedelta(days=365)
        new_data = [kill for kill in processed_kills.values() if kill["date"] >= cutoff]
        if skipped_char_ids:
            logger.info(
                "[AWOX] Skipped %d character(s) for user %s due to zKill rate limit; will retry next cycle.",
                len(skipped_char_ids),
                user_id,
            )
            new_data = _merge_cached_kills(existing_data, new_data, cutoff)
        else:
            new_data.sort(key=lambda x: x["date"], reverse=True)

        # Update cache
        AwoxKillsCache.objects.update_or_create(
            user_id=user_id, defaults={"data": _serialize_datetime(new_data), "last_accessed": now}
        )
        return new_data

    except Exception as e:
        logger.exception(f"Error in fetch_awox_kills for user {user_id}: {e}")
        return existing_data
    finally:
        session.close()


def render_awox_kills_html(userID):
    """
    Render the cached awox data into a simple Bootstrap friendly table.

    Returning the standardized empty message allows for consistent UI.
    """
    kills = fetch_awox_kills(userID)
    if not kills:  # Nothing to render, return standardized empty table.
        return '<table class="table stats"><tbody><tr><td class="text-center">No recent AWOX kills found.</td></tr></tbody></table>'

    html_output = '<table class="table table-striped table-hover stats">'
    html_output += "<thead><tr><th>Date</th><th>Character(s)</th><th>Attacker</th><th>Victim</th><th>Value</th><th>Link</th></tr></thead><tbody>"

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

        if kill.get("is_attacker", False):
            row_html = '<tr class="text-danger"><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{} ISK</td><td><a href="{}" target="_blank">View</a></td></tr>'
            html_output += format_html(
                row_html, date_str, chars, mark_safe(att_html), mark_safe(vic_html), value, link
            )

    html_output += "</tbody></table>"
    return html_output


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

        results.append(
            {
                "link": kill["link"],
                "date": date_str,
                "value": "{:,}".format(kill.get("value", 0)),
                "is_attacker": kill.get("is_attacker", False),
            }
        )

    return results
