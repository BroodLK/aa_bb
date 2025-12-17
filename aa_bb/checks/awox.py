"""
Fetches and caches "awox" killmails (friendly fire) for a user's characters.

The functions here encapsulate the networking against zKillboard/ESI,
cache management, and rendering helpers so the calling views do not have to
care about throttling or HTML generation.
"""

import requests
import time
import logging
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from allianceauth.authentication.models import CharacterOwnership

from ..models import BigBrotherConfig,AwoxKillsCache
from django.utils import timezone
from esi.exceptions import HTTPNotModified
from ..esi_client import esi, call_result
from ..app_settings import (
    DATASOURCE,
    esi_tenant_kwargs,
    get_site_url,
    get_contact_email,
    get_owner_name,
    send_message,
    resolve_alliance_name,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

USER_AGENT = f"{get_site_url()} Maintainer: {get_owner_name()} {get_contact_email()}"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip",
    "Accept": "application/json",
}

def _get_corp_name(corp_id):
    if not corp_id:
        return "None"
    try:
        operation = esi.client.Corporation.GetCorporationsCorporationId(corporation_id=corp_id)
        result, _ = call_result(operation)
        return result.get("name", f"Unknown ({corp_id})")
    except Exception:
        return f"Unknown ({corp_id})"

# Limit zKill "down" notifications to once every 2 hours
_last_zkill_down_notice_monotonic = 0.0


def _notify_zkill_down_once(preview: str, status: int | None, content_type: str | None):
    """
    Fire a single Discord notification when zKill returns junk.

    An in-memory timestamp prevents duplicate warnings within the last
    ~hour so alerts do not spam when zKill is flaking.
    """
    global _last_zkill_down_notice_monotonic
    now = time.monotonic()
    # 2 hours = 7200 seconds
    if now - _last_zkill_down_notice_monotonic < 3500:  # Skip notification if a warning was sent recently.
        return
    _last_zkill_down_notice_monotonic = now
    msg = (
        "zKillboard appears unavailable and awox checks will not work (non-JSON response).\n"
        f"status={status} content_type='{content_type}'\n"
        f"body preview: ```{preview}```"
    )
    try:
        awox_notify = BigBrotherConfig.awox_notify
        if awox_notify:
            send_message(msg)
    except Exception as e:
        logger.warning(f"Failed to send zKill down notification: {e}")


def fetch_awox_kills(user_id, delay=0.2):
    """
    Return a deduplicated list of awox kill summaries for the given user.

    A DB cache keeps checklist reloads cheap; otherwise each character's
    recent awox activity is pulled from zKill, the full mail is hydrated
    via ESI, and the resulting summary is cached for future calls.
    """
    # Indefinite DB cache: return cached kills if present
    try:
        cache = AwoxKillsCache.objects.get(pk=user_id)
        try:
            cache.last_accessed = timezone.now()
            cache.save(update_fields=["last_accessed"])
        except Exception:
            cache.save()
        return cache.data or []
    except AwoxKillsCache.DoesNotExist:
        pass
    characters = CharacterOwnership.objects.filter(user__id=user_id)
    char_ids = [c.character.character_id for c in characters]
    char_id_map = {c.character.character_id: c.character.character_name for c in characters}


    kills_by_id = {}

    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers.update({"Connection": "close"})
    retries = Retry(total=3, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))

    for char_id in char_ids:
        zkill_url = f"https://zkillboard.com/api/characterID/{char_id}/awox/1/"
        try:
            response = session.get(zkill_url, timeout=(3, 10))
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Error fetching awox for {char_id}: {e}")
            continue

        # zKill may return HTML/Cloudflare challenge or a custom error page
        content_type = response.headers.get("Content-Type", "")
        text_preview = (response.text or "").strip()[:200]
        text_lower = (response.text or "").lower()
        if (
            not content_type.startswith("application/json")
            or "so a big oops happened" in text_lower
            or "cdn-cgi/challenge-platform" in text_lower
        ):  # Bail out when zKill returns HTML (Cloudflare/error) instead of JSON payloads.
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

        for kill in killmails:
            kill_id = kill.get("killmail_id")
            hash_ = kill.get("zkb", {}).get("hash")
            value = kill.get("zkb", {}).get("totalValue", 0)

            if not kill_id or not hash_:  # Ignore malformed entries that lack identifiers.
                continue
            if kill_id in kills_by_id:  # Skip duplicates pulled from multiple characters.
                continue

            operation = esi.client.Killmails.GetKillmailsKillmailIdKillmailHash(
                killmail_id=kill_id,
                killmail_hash=hash_,
                **esi_tenant_kwargs(DATASOURCE),
            )
            try:
                full_kill, _ = call_result(operation)
            except HTTPNotModified:
                full_kill, _ = call_result(operation, use_etag=False)
            except Exception as e:
                logger.warning("Failed to fetch ESI killmail %s: %s", kill_id, e)
                continue

            attackers = full_kill.get("attackers", [])
            victim = full_kill.get("victim", {})
            victim_id = victim.get("character_id")

            attacker_names = set()
            attacker_affiliations = []

            for attacker in attackers:
                a_id = attacker.get("character_id")
                if a_id in char_ids and a_id != victim_id:  # Friendly fire only counts when attacker differs from victim.
                    attacker_names.add(char_id_map.get(a_id))
                    attacker_affiliations.append({
                        "corp_id": attacker.get("corporation_id"),
                        "alliance_id": attacker.get("alliance_id")
                    })

            if not attacker_names:  # No awox behaviour detected for this killmail.
                continue

            # Resolve names
            att_info = attacker_affiliations[0]
            att_corp = _get_corp_name(att_info["corp_id"])
            att_alli = resolve_alliance_name(att_info["alliance_id"]) if att_info["alliance_id"] else None

            vic_corp_id = victim.get("corporation_id")
            vic_alli_id = victim.get("alliance_id")
            vic_corp = _get_corp_name(vic_corp_id)
            vic_alli = resolve_alliance_name(vic_alli_id) if vic_alli_id else None

            kills_by_id[kill_id] = {
                "value": int(value),
                "link": f"https://zkillboard.com/kill/{kill_id}/",
                "chars": attacker_names,
                "att_corp": att_corp,
                "att_alli": att_alli,
                "vic_corp": vic_corp,
                "vic_alli": vic_alli,
                "date": full_kill.get("killmail_time"),
            }

    data_list = list(kills_by_id.values()) if kills_by_id else []
    try:
        AwoxKillsCache.objects.update_or_create(
            user_id=user_id,
            defaults={"data": data_list, "last_accessed": timezone.now()},
        )
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

        att_html = kill.get("att_corp", "")
        if kill.get("att_alli"):
            att_html += f"<br><small>({kill.get('att_alli')})</small>"

        vic_html = kill.get("vic_corp", "")
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
    if not kills:  # No cached kills yet; callers expect empty list.
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
