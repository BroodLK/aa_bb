"""
Core helper utilities shared across BigBrother and its companion modules.

This module wraps ESI, caches, and AllianceAuth integration so the rest of
the codebase can fetch character/corp data, resolve names, and emit Discord
messages without duplicating all the plumbing.
"""
from collections import deque

from allianceauth.authentication.models import UserProfile, CharacterOwnership
import logging
import re
import subprocess
import sys
import requests
from datetime import datetime, timedelta

from django.apps import apps
from django.utils import timezone
from typing import Optional, Dict, Tuple, Any, List
from django.db import transaction, IntegrityError, OperationalError

from .models import (
    Alliance_names, Corporation_names, Character_names, BigBrotherConfig, id_types,
    EntityInfoCache, CharacterEmploymentCache, CorporationInfoCache, AllianceHistoryCache, SovereigntyMapCache,
)

from dateutil.parser import parse as parse_datetime
import time
from httpx import RequestError
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from django.contrib.auth import get_user_model
from allianceauth.framework.api.user import get_main_character_name_from_user
from esi.exceptions import HTTPClientError, HTTPServerError, HTTPNotModified
from .esi_client import esi, to_plain, call_result, call_results, parse_expires
from .esi_cache import expiry_cache_key, get_cached_expiry, set_cached_expiry
from allianceauth.eveonline.models import EveCorporationInfo, EveAllianceInfo
from eveuniverse.models import EveSolarSystem
from django.core.cache import cache
from django.conf import settings

logger = logging.getLogger(__name__)

DATASOURCE = "tranquility"
VERBOSE_WEBHOOK_LOGGING = True


def esi_tenant_kwargs(datasource: str | None):
    """
    Translate the legacy datasource argument into the new X-Tenant header expected by
    the aiopenapi3-powered ESI client.
    """
    tenant = datasource or DATASOURCE
    return {"X_Tenant": tenant} if tenant else {}


def _resolve_names_via_esi(ids: list[int]) -> dict[int, str]:
    """
    Resolve a list of EVE IDs into their names using /universe/names via the
    OpenAPI client. Returns a dict mapping id -> name.
    """
    if not ids:  # Nothing to resolve when the caller supplied no IDs.
        return {}
    operation = esi.client.Universe.PostUniverseNames(
        body=ids,
        **esi_tenant_kwargs(DATASOURCE),
    )
    try:
        rows = to_plain(operation.result())
    except HTTPNotModified:
        rows = to_plain(operation.result(use_etag=False))
    return {
        int(row.get("id")): row.get("name")
        for row in (rows or [])
        if row.get("id") is not None
    }



# Owner-name cache (7d TTL)
_owner_name_cache: Dict[int, Tuple[str, datetime]] = {}

def get_pings(message_type: str) -> str:
    """
    Given a MessageType instance, return a string of pings separated by spaces.
    """
    cfg = BigBrotherConfig.get_solo()
    pings = []

    if cfg.pingrole1_messages.all().filter(name=message_type).exists():  # Ping role1 when message type is subscribed.
        pings.append(f"<@&{cfg.pingroleID}>")

    if cfg.pingrole2_messages.all().filter(name=message_type).exists():  # Ping role2 when configured.
        pings.append(f"<@&{cfg.pingroleID2}>")

    if cfg.here_messages.all().filter(name=message_type).exists():  # Include @here when enabled.
        pings.append("@here")

    if cfg.everyone_messages.all().filter(name=message_type).exists():  # Include @everyone when enabled.
        pings.append("@everyone")

    ping = " " + " ".join(pings) if pings else ""

    return ping

def _find_employment_at(employment: List[dict], date: datetime) -> Optional[dict]:
    """Return the employment record active at the provided datetime."""
    for rec in employment:
        start = rec.get('start_date')
        end = rec.get('end_date')
        if start and start <= date and (end is None or date < end):  # Overlap indicates active stint at the target time.
            return rec
    return None

def get_main_character_name(user_id):
    """Convenience wrapper returning the AA profile's main character name."""
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
        return get_main_character_name_from_user(user)
    except User.DoesNotExist:
        return None

def _find_alliance_at(history: List[dict], date: datetime) -> Optional[int]:
    """Return the alliance id active for the corp at the given time."""
    for i, rec in enumerate(history):
        start = rec.get('start_date')
        next_start = history[i+1]['start_date'] if i+1 < len(history) else None
        if start and start <= date and (next_start is None or date < next_start):  # Period overlaps the requested timestamp.
            return rec.get('alliance_id')
    return None

def get_eve_entity_type_int(eve_id: int, datasource: str | None = None) -> str | None:
    """
    Resolve an EVE Online ID to its entity type.

    Returns:
        'character', 'corporation', 'alliance', etc., or None on error/not found.
    """
    if eve_id is None:  # Guard callers that pass falsy IDs.
        logging.warning("No EVE ID provided to get_eve_entity_type_int")
        return None
    max_retries = 3
    delay_seconds = 0.5
    results = None

    for attempt in range(1, max_retries + 1):
        try:
            operation = esi.client.Universe.PostUniverseNames(
                body=[eve_id],
                **esi_tenant_kwargs(datasource),
            )
            try:
                results = to_plain(operation.result())
            except HTTPNotModified:
                results = to_plain(operation.result(use_etag=False))
            break
        except (HTTPClientError, HTTPServerError) as exc:
            logger.warning(f"ESI error resolving {eve_id}: {exc}")
            return None
        except (RequestError, requests.exceptions.RequestException) as exc:
            logger.warning(
                "Transient ESI connection issue while resolving %s "
                "(attempt %s/%s): %s",
                eve_id,
                attempt,
                max_retries,
                exc,
            )
            if attempt == max_retries:  # Exhausted retries; surface failure.
                return None
            time.sleep(delay_seconds * attempt)

    if not results:  # Nothing was returned from ESI.
        return None
    return results[0].get("category")

def get_eve_entity_type(
    eve_id: int,
    datasource: str | None = None
) -> Optional[str]:
    """
    Resolve an EVE Online ID to its entity type, caching results in the `id_types` table.

    Workflow:
      1. Try to get a cached record via id_types.objects.get(pk=eve_id).
      2. If found, return record.name.
      3. On DoesNotExist, call get_eve_entity_type() to fetch from ESI.
      4. If ESI returns a non-null type, save a new id_types record.
      5. Return the resolved type (or None if unresolved).
    """
    # 1. Cache lookup
    try:
        record = id_types.objects.get(pk=eve_id)
        # mark last access time without touching freshness timestamp
        try:
            record.last_accessed = timezone.now()
            record.save(update_fields=["last_accessed"])
        except Exception:
            record.save()
        return record.name
    except id_types.DoesNotExist:
        pass

    # 2. Cache miss — resolve via ESI
    entity_type = get_eve_entity_type_int(eve_id, datasource=datasource)
    if entity_type is None:  # ESI could not resolve the ID.
        return None

    # 3. Store in cache
    try:
        with transaction.atomic():
            obj = id_types(id=eve_id, name=entity_type)
            obj.save()
    except IntegrityError:
        # another thread/process inserted it first; safe to ignore
        logging.debug(f"ID {eve_id} was cached by another process.")

    return entity_type

def is_npc_character(character_id: int) -> bool:
    """Check whether a character id falls inside the NPC character range."""
    return 3_000_000 <= character_id < 4_000_000

def get_character_id(name: str) -> int | None:
    """
    Resolve a character name to ID using ESI /universe/ids/ endpoint,
    with caching implemented through the Django model. Uses `esi.client` and
    self-heals duplicate name rows by reconciling via ESI.
    """
    # Step 1: Fast-path from DB when exactly one record exists
    try:
        record = Character_names.objects.get(name=name)
    except Character_names.MultipleObjectsReturned:
        record = None  # fall through to ESI reconciliation below
    except Character_names.DoesNotExist:
        record = None
    else:
        record.updated = timezone.now()
        record.save()
        return record.id

    # Step 2: Resolve via ESI and reconcile duplicates
    operation = esi.client.Universe.PostUniverseIds(
        body=[str(name)],
        **esi_tenant_kwargs(DATASOURCE),
    )
    try:
        data = to_plain(operation.result())
    except HTTPNotModified:
        data = to_plain(operation.result(use_etag=False))
    except (HTTPClientError, HTTPServerError) as e:
        logger.error(f"ESI error resolving character name '{name}': {e}")
        # Fallback to most recent local record if present
        fallback = (
            Character_names.objects
            .filter(name=name)
            .order_by("-updated")
            .first()
        )
        if fallback:  # Cached name available when ESI fails; reuse stored entry.
            fallback.updated = timezone.now()
            fallback.save()
            return fallback.id
        return None

    characters = (data or {}).get("characters", [])
    if not characters:  # No match returned from ESI.
        return None

    char_id = int(characters[0]["id"])

    # Ensure canonical mapping exists
    with transaction.atomic():
        obj, created = Character_names.objects.get_or_create(
            id=char_id,
            defaults={"name": name}
        )
        if not created and obj.name != name:  # Update stale entries when ESI says the canonical name changed.
            obj.name = name
            obj.updated = timezone.now()
            obj.save()

    # Proactively fix any duplicate rows left over with the same name but different IDs
    try:
        stale_qs = Character_names.objects.filter(name=name).exclude(id=char_id)
        if stale_qs.exists():  # Duplicate rows detected; clean them up.
            try:
                # Resolve correct names for stale IDs using ESI
                stale_ids = [int(s.id) for s in stale_qs]
                name_future = esi.client.Universe.PostUniverseNames(
                    body=stale_ids,
                    **esi_tenant_kwargs(DATASOURCE),
                )
                try:
                    name_data = to_plain(name_future.result())
                except HTTPNotModified:
                    name_data = to_plain(name_future.result(use_etag=False))
                name_rows = {
                    int(r.get("id")): r.get("name")
                    for r in (name_data or [])
                }
            except (HTTPClientError, HTTPServerError):
                name_rows = {}

            for stale in stale_qs:
                correct_name = name_rows.get(int(stale.id)) or stale.name
                if correct_name != stale.name:  # Rename rows resolved to a different canonical name.
                    stale.name = correct_name
                    stale.updated = timezone.now()
                    stale.save()
    except Exception as e:
        logger.debug(f"Duplicate cleanup failed for name='{name}': {e}")

    return char_id

_EXPIRY = timedelta(days=30)

def get_entity_info(entity_id: int, as_of: timezone.datetime) -> Dict:
    """
    Returns a dict:
      {
        'name': str,
        'type': 'character'|'corporation'|'alliance'|None,
        'corp_id': Optional[int],
        'corp_name': str,
        'alli_id': Optional[int],
        'alli_name': str,
      }
    Caches the result in the DB for 2 hours.
    """
    if entity_id is None:  # Replace missing IDs with placeholder to avoid crashing downstream.
        entity_id = 342545170
        errent = True
    else:
        errent = False
    now = timezone.now()

    # 1) Attempt to fetch fresh-enough cache entry
    try:
        cache = EntityInfoCache.objects.get(entity_id=entity_id, as_of=as_of)
        cache.updated = timezone.now()
        cache.save()
        if now - cache.updated < _EXPIRY:  # Serve cached data when still within TTL.
            #logger.debug(f"cache hit: entity={entity_id} @ {as_of}")
            return cache.data
        else:
            #logger.debug(f"cache stale: entity={entity_id} @ {as_of}, expired {cache.updated}")
            cache.delete()
    except EntityInfoCache.DoesNotExist:
        pass
    #logger.debug(f"cache empty: entity={entity_id} @ {as_of}")

    # 2) Compute fresh info
    etype = get_eve_entity_type(entity_id)
    name = corp_name = alli_name = "-"
    corp_id = alli_id = None

    if etype == "character":  # Character IDs need corp/alliance context via employment.
        name = resolve_character_name(entity_id)
        emp = get_character_employment(entity_id)
        rec = _find_employment_at(emp, as_of)
        if rec:  # Employment record found for timestamp, populate corp/alli metadata.
            corp_id   = rec["corporation_id"]
            corp_name = rec["corporation_name"]
            alli_id   = _find_alliance_at(rec.get("alliance_history", []), as_of)
            if alli_id:  # Resolve alliance name when an alliance id exists.
                alli_name = resolve_alliance_name(alli_id)

    elif etype == "corporation":  # Corp IDs only need alliance info via history.
        corp_id   = entity_id
        corp_name = resolve_corporation_name(entity_id)
        hist      = get_alliance_history_for_corp(entity_id)
        alli_id   = _find_alliance_at(hist, as_of)
        if alli_id:  # Lookup the alliance name when the corp was in one.
            alli_name = resolve_alliance_name(alli_id)

    elif etype == "alliance":  # Alliance IDs only require name resolution.
        alli_id   = entity_id
        alli_name = resolve_alliance_name(entity_id)

    info = {
        "name":      name,
        "type":      etype,
        "corp_id":   corp_id,
        "corp_name": corp_name,
        "alli_id":   alli_id,
        "alli_name": alli_name,
    }

    # 3) Store in cache table (create or update)
    #    wrap in transaction to avoid race conditions
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        try:
            with transaction.atomic():
                EntityInfoCache.objects.update_or_create(
                    entity_id=entity_id,
                    as_of=as_of,
                    defaults={"data": info}
                )
            break  # Success, exit loop
        except OperationalError as e:
            if 'Deadlock' in str(e) and attempt < MAX_RETRIES - 1:  # Retry transient deadlocks with exponential backoff.
                time.sleep(0.1 * (attempt + 1))  # small backoff
                continue
            raise

    if errent:  # Flag placeholder lookups so downstream consumers know input was missing.
        errmsg = "Error: entity id provided is None "
        info = {
            "name":      errmsg,
            "type":      etype,
            "corp_id":   corp_id,
            "corp_name": errmsg,
            "alli_id":   alli_id,
            "alli_name": errmsg,
        }

    return info

TTL_SHORT = timedelta(hours=4)

def _ser_dt(v):
    """Serialize datetime objects to ISO strings for JSON storage."""
    return v.isoformat() if isinstance(v, datetime) else v

def _deser_dt(v):
    """Inverse of _ser_dt; tolerate both ISO strings and already-parsed datetimes."""
    if isinstance(v, str):  # Convert any ISO-ish strings back into datetime objects.
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            try:
                return parse_datetime(v)
            except Exception:
                return v
    return v

def _ser_employment(rows: list[dict]) -> list[dict]:
    """Normalize employment rows before storing them in the cache table."""
    out = []
    for r in rows:
        out.append({
            'corporation_id': r.get('corporation_id'),
            'corporation_name': r.get('corporation_name'),
            'start_date': _ser_dt(r.get('start_date')),
            'end_date': _ser_dt(r.get('end_date')),
            'alliance_history': [
                {'alliance_id': ah.get('alliance_id'), 'start_date': _ser_dt(ah.get('start_date'))}
                for ah in (r.get('alliance_history') or [])
            ],
        })
    return out

def _deser_employment(rows: list[dict]) -> list[dict]:
    """Hydrate employment-cache rows back into Python objects."""
    out = []
    for r in rows or []:
        out.append({
            'corporation_id': r.get('corporation_id'),
            'corporation_name': r.get('corporation_name'),
            'start_date': _deser_dt(r.get('start_date')),
            'end_date': _deser_dt(r.get('end_date')),
            'alliance_history': [
                {'alliance_id': ah.get('alliance_id'), 'start_date': _deser_dt(ah.get('start_date'))}
                for ah in (r.get('alliance_history') or [])
            ],
        })
    return out

def get_character_employment(character_or_id) -> list[dict]:
    """
    Fetch and format the permanent employment history for a character.
    Accepts either:
      - an int: the EVE character_id
      - an object with .character_id attribute
    Returns a list of dicts:
      {
        'corporation_id': int,
        'corporation_name': str,
        'start_date': datetime,
        'end_date': datetime|None,
        'alliance_history': [ {'alliance_id': int, 'start_date': datetime}, ... ]
      }
    On ESI failure, logs and returns [].
    """
    # 1. Normalize to integer character_id
    if isinstance(character_or_id, int):  # Accept raw IDs directly.
        char_id = character_or_id
    else:
        try:
            char_id = int(character_or_id.character_id)
        except (AttributeError, TypeError, ValueError):
            raise ValueError(
                "get_character_employment() requires an int or an object with .character_id"
            )

    # 2. Cache: try DB (4h TTL)
    expiry_key = expiry_cache_key("char_emp", char_id)
    expiry_hint = get_cached_expiry(expiry_key)
    cache_entry = None
    cached_rows = None
    try:
        ce = CharacterEmploymentCache.objects.get(pk=char_id)
        cache_entry = ce
        cached_rows = _deser_employment(ce.data)
        now_ts = timezone.now()
        if expiry_hint and expiry_hint > now_ts:  # Cache still valid per redis hint.
            return cached_rows
        if expiry_hint is None and now_ts - ce.updated < TTL_SHORT:  # Fall back to DB timestamp TTL.
            try:
                ce.last_accessed = timezone.now()
                ce.save(update_fields=['last_accessed'])
            except Exception:
                ce.save()
            return cached_rows
    except CharacterEmploymentCache.DoesNotExist:
        cache_entry = None

    # 3. Fetch the corp history from ESI
    operation = esi.client.Character.GetCharactersCharacterIdCorporationhistory(
        character_id=char_id
    )
    try:
        response, new_expiry = call_results(operation)
        set_cached_expiry(expiry_key, new_expiry)
    except HTTPNotModified as exc:
        set_cached_expiry(expiry_key, parse_expires(getattr(exc, "headers", {})))
        if cache_entry:  # Use DB cache when ESI returned 304.
            try:
                cache_entry.updated = timezone.now()
                cache_entry.last_accessed = timezone.now()
                cache_entry.save(update_fields=["updated", "last_accessed"])
            except Exception:
                cache_entry.save()
            return cached_rows or _deser_employment(cache_entry.data)
        logger.debug("ESI returned 304 for char %s but no cache available", char_id)
        response, new_expiry = call_results(operation, use_etag=False)
        set_cached_expiry(expiry_key, new_expiry)
    except Exception as e:
        logger.exception(f"ESI failure for character_id {char_id}: {e}")
        return []

    # 4. Order from earliest to latest
    history = list(reversed(response))
    rows = []

    for idx, membership in enumerate(history):
        corp_id = membership.get('corporation_id')
        if not corp_id or is_npc_corporation(corp_id):  # Skip NPC corps or missing ids.
            continue

        start = ensure_datetime(membership.get('start_date'))
        # Next start_date becomes this membership's end_date
        end = None
        if idx + 1 < len(history):  # Next row's start becomes this row's end.
            end = ensure_datetime(history[idx + 1].get('start_date'))

        # Enrich with corp and alliance info
        corp_info     = get_corporation_info(corp_id)
        alliance_hist = get_alliance_history_for_corp(corp_id)

        rows.append({
            'corporation_id':   corp_id,
            'corporation_name': corp_info.get('name'),
            'start_date':       start,
            'end_date':         end,
            'alliance_history': alliance_hist,
        })

        # Persist the corporation name for future lookups
        with transaction.atomic():
            Corporation_names.objects.update_or_create(
                pk=corp_id,
                defaults={'name': corp_info.get('name', f"Unknown ({corp_id})")}
            )

    # Save to cache
    try:
        CharacterEmploymentCache.objects.update_or_create(
            char_id=char_id,
            defaults={'data': _ser_employment(rows), 'last_accessed': timezone.now()},
        )
    except Exception:
        pass
    return rows

def get_user_characters(user_id: int) -> dict[int, str]:
    """Return {character_id: character_name} for the given AllianceAuth user."""
    qs = CharacterOwnership.objects.filter(user__id=user_id).select_related('character')
    return {
        co.character.character_id: co.character.character_name
        for co in qs
    }

def format_int(value: int) -> str:
    """
    Format an integer SP value using dots as thousands separators.
    E.g. 65861521 → "65.861.521"
    """
    # Python’s built-in uses commas; swap them out for dots
    return f"{value:,}".replace(",", ".")

def is_npc_corporation(corp_id):
    """Return True when the corporation id falls inside the NPC range."""
    return 1_000_000 <= corp_id < 2_000_000

CORP_TTL = timedelta(hours=4)

def get_corporation_info(corp_id):
    """
    Fetch corporation info from DB cache or ESI (24h TTL).
    """
    expiry_key = expiry_cache_key("corp_info", corp_id)
    expiry_hint = get_cached_expiry(expiry_key)
    # 1) Try DB cache first
    cached_entry = None
    try:
        entry = CorporationInfoCache.objects.get(pk=corp_id)
        cached_entry = entry
        now_ts = timezone.now()
        if expiry_hint and expiry_hint > now_ts:  # Cached corp info still valid according to redis.
            return {"name": entry.name, "member_count": entry.member_count}
        if expiry_hint is None and now_ts - entry.updated < CORP_TTL:  # Fall back to DB timestamp TTL when redis hint missing.
            return {"name": entry.name, "member_count": entry.member_count}
    except CorporationInfoCache.DoesNotExist:
        entry = None

    # 2) Fetch fresh from ESI
    try:
        operation = esi.client.Corporation.GetCorporationsCorporationId(
            corporation_id=corp_id
        )
        result, expires_at = call_result(operation)
        set_cached_expiry(expiry_key, expires_at)
        info = {
            "name": result.get("name", f"Unknown ({corp_id})"),
            "member_count": result.get("member_count", 0),
        }
    except HTTPNotModified as exc:
        set_cached_expiry(expiry_key, parse_expires(getattr(exc, "headers", {})))
        if cached_entry:  # Serve stale entry if ESI returned 304.
            cached_entry.updated = timezone.now()
            cached_entry.save(update_fields=["updated"])
            return {"name": cached_entry.name, "member_count": cached_entry.member_count}
        logger.debug("ESI returned 304 for corp %s but no cache exists", corp_id)
        try:
            result, expires_at = call_result(operation, use_etag=False)
            set_cached_expiry(expiry_key, expires_at)
            return {
                "name": result.get("name", f"Unknown ({corp_id})"),
                "member_count": result.get("member_count", 0),
            }
        except Exception:
            return {"name": f"Unknown Corp ({corp_id})", "member_count": 0}
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:
        print(f"Failed to fetch corp info [{corp_id}]: {e}")
        info = {"name": f"Unknown Corp ({corp_id})", "member_count": 0}

    # 3) Store/update DB cache
    CorporationInfoCache.objects.update_or_create(
        corp_id=corp_id,
        defaults=info
    )

    return info


def ensure_datetime(value):
    """Best-effort conversion of ISO strings into timezone-aware datetimes."""
    if isinstance(value, str):  # Parse ISO strings as timezone-aware datetimes.
        return parse_datetime(value)
    return value

def _fetch_alliance_history(corp_id, expiry_key, cached_history=None):
    """Wrapper around the alliance-history endpoint that respects caching hints."""
    operation = esi.client.Corporation.GetCorporationsCorporationIdAlliancehistory(
        corporation_id=corp_id
    )
    try:
        data, expires_at = call_results(operation)
        set_cached_expiry(expiry_key, expires_at)
        return data
    except HTTPNotModified as exc:
        set_cached_expiry(expiry_key, parse_expires(getattr(exc, "headers", {})))
        if cached_history is not None:  # Use cached history when ESI returns 304.
            return cached_history
        data, expires_at = call_results(operation, use_etag=False)
        set_cached_expiry(expiry_key, expires_at)
        return data
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch alliance history for corp {corp_id}: {e}")
        return []

logger = logging.getLogger(__name__)

ALLIANCE_TTL = timedelta(hours=24)

def _parse_datetime(value):
    """Parse ISO8601 string to datetime, return None if invalid."""
    if isinstance(value, datetime):  # Already parsed datetimes pass through untouched.
        return value
    if isinstance(value, str):  # Attempt to parse ISO strings.
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None

def _serialize_datetime(value):
    """Recursively convert datetime objects to ISO8601 strings."""
    if isinstance(value, datetime):  # Serialize datetime objects to strings.
        return value.isoformat()
    if isinstance(value, list):  # Recurse into lists.
        return [_serialize_datetime(v) for v in value]
    if isinstance(value, dict):  # Recurse into dicts.
        return {k: _serialize_datetime(v) for k, v in value.items()}
    return value

def get_alliance_history_for_corp(corp_id):
    """Return chronological alliance-history entries for the given corporation."""
    # 1) Try DB cache first
    cached_history = None
    expiry_key = expiry_cache_key("corp_alliance_history", corp_id)
    expiry_hint = get_cached_expiry(expiry_key)
    try:
        entry = AllianceHistoryCache.objects.get(pk=corp_id)
        cached_history = [
            {
                "alliance_id": h.get("alliance_id"),
                "start_date": _parse_datetime(h.get("start_date")),
            }
            for h in entry.history
        ]
        now_ts = timezone.now()
        if expiry_hint and expiry_hint > now_ts:  # Cache still valid according to redis hint.
            return cached_history
        if expiry_hint is None and entry.is_fresh:  # DB entry recently refreshed; reuse.
            return cached_history
        entry.delete()
    except AllianceHistoryCache.DoesNotExist:
        pass

    # 2) Fetch fresh directly
    history = []
    try:
        response = _fetch_alliance_history(
            corp_id,
            expiry_key=expiry_key,
            cached_history=cached_history,
        )
        history = [
            {
                "alliance_id": h.get("alliance_id"),
                "start_date": _parse_datetime(h.get("start_date")),
            }
            for h in response
        ]
        history.sort(key=lambda x: x["start_date"] or datetime.min)
    except Exception as e:
        logger.info(f"Error fetching alliance history for corp {corp_id}: {e}")
        return []

    # 3) Store in DB (serialize datetimes as strings)
    serialized_history = _serialize_datetime(history)
    AllianceHistoryCache.objects.update_or_create(
        corp_id=corp_id,
        defaults={"history": serialized_history}
    )

    return history

def _get_sov_map() -> list:
    """Fetch (and cache) the sovereignty map used by get_system_owner."""
    entry = None
    try:
        entry = SovereigntyMapCache.objects.get(pk=1)
        if entry.is_fresh:  # Use cached sovereignty map when still fresh.
            return entry.data
    except SovereigntyMapCache.DoesNotExist:
        pass

    operation = esi.client.Sovereignty.GetSovereigntyMap(
        **esi_tenant_kwargs(DATASOURCE),
    )
    try:
        data, _ = call_results(operation)
    except HTTPNotModified:
        if entry:  # Serve cached data on 304 responses when cache exists.
            try:
                entry.updated = timezone.now()
                entry.save(update_fields=["updated"])
            except Exception:
                entry.save()
            return entry.data
        data, _ = call_results(operation, use_etag=False)

    SovereigntyMapCache.objects.update_or_create(
        pk=1,
        defaults={"data": data}
    )

    return data

def resolve_alliance_name(owner_id: int) -> str:
    """
    Resolve alliance/faction ID to name via ESI, storing permanently in aa_bb_alliances.
    On lookup failure, falls back to stale DB record or returns 'Unresolvable <Error>'.
    """
    # 1. Try permanent table first
    try:
        record = Alliance_names.objects.get(pk=owner_id)
        record.updated = timezone.now()
        record.save()
        return record.name
    except Alliance_names.DoesNotExist:
        pass  # need to fetch and store

    # 2. Fetch from ESI
    try:
        name_map = _resolve_names_via_esi([owner_id])
        owner_name = name_map.get(owner_id) or "Unresolvable"

        # 3. Save or update the DB record
        with transaction.atomic():
            Alliance_names.objects.update_or_create(
                pk=owner_id,
                defaults={"name": owner_name}
            )

        return owner_name

    except Exception as e:
        # 4. On error, log and fallback to stale if any
        logger.exception(f"Failed to resolve name for owner ID {owner_id}: {e}")
        try:
            stale = Alliance_names.objects.get(pk=owner_id)
            return stale.name
        except Alliance_names.DoesNotExist:
            pass

        e_short  = e.__class__.__name__
        e_detail = getattr(e, 'code', None) or getattr(e, 'status', None) or str(e)
        return f"Unresolvable eve map{e_short}{e_detail}"

def resolve_corporation_name(corp_id: int) -> str:
    """
    Resolve corporation ID to name via ESI, storing permanently in aa_bb_corporations.
    On lookup failure, falls back to stale DB record or returns 'Unresolvable <Error>'.
    """
    # 1. Try permanent table first
    try:
        record = Corporation_names.objects.get(pk=corp_id)
        record.updated = timezone.now()
        record.save()
        return record.name
    except Corporation_names.DoesNotExist:
        pass  # need to fetch and store

    # 2. Fetch from ESI
    try:
        name_map = _resolve_names_via_esi([corp_id])
        corp_name = name_map.get(corp_id) or "Unresolvable"

        # 3. Save or update the DB record
        with transaction.atomic():
            Corporation_names.objects.update_or_create(
                pk=corp_id,
                defaults={"name": corp_name}
            )

        return corp_name

    except Exception as e:
        # 4. On error, log and fallback to stale if any
        logger.exception(f"Failed to resolve name for corporation ID {corp_id}: {e}")
        try:
            stale = Corporation_names.objects.get(pk=corp_id)
            return stale.name
        except Corporation_names.DoesNotExist:
            pass

        e_short  = e.__class__.__name__
        e_detail = getattr(e, 'code', None) or getattr(e, 'status', None) or str(e)
        return f"Unresolvable eve map{e_short}{e_detail}"

def resolve_character_name(char_id: int) -> str:
    """
    Resolve character ID to name via ESI, storing permanently in Character_names.
    On lookup failure, falls back to stale DB record or returns 'Unresolvable <Error>'.
    """
    # 1. Try permanent table first
    try:
        record = Character_names.objects.get(pk=char_id)
        record.updated = timezone.now()
        record.save()
        return record.name
    except Character_names.DoesNotExist:
        pass  # need to fetch and store

    # 2. Fetch from ESI
    try:
        name_map = _resolve_names_via_esi([char_id])
        char_name = name_map.get(char_id) or "Unresolvable"

        # 3. Save or update the DB record
        with transaction.atomic():
            Character_names.objects.update_or_create(
                pk=char_id,
                defaults={"name": char_name}
            )

        return char_name

    except Exception as e:
        # 4. On error, log and fallback to stale if any
        logger.exception(f"Failed to resolve name for character ID {char_id}: {e}")
        try:
            stale = Character_names.objects.get(pk=char_id)
            return stale.name
        except Character_names.DoesNotExist:
            pass

        e_short = e.__class__.__name__
        e_detail = getattr(e, 'code', None) or getattr(e, 'status', None) or str(e)
        return f"Unresolvable eve map{e_short}{e_detail}"


def get_system_owner(system: str) -> Dict[str, str]:
    """
    Get sovereignty owner of an EVE system by name.
    Always returns a dict with keys: owner_id, owner_name, owner_type.
    """
    owner_id = "0"
    owner_name = f"Unresolvable Init"
    owner_type = "unknown"

    # 1) Pull name and ID from the passed-in dict
    system_id = system.get("id")
    system_nam = system.get("name")
    system_name = str()
    if system_nam:  # Convert the provided name into a proper string when available.
        system_name = str(system_nam)

    try:
        sov_map = _get_sov_map()
        entry = next((s for s in sov_map if s.get("system_id") == system_id), None)
        if not entry:
            return {"owner_id": owner_id, "owner_name": f"Unresolvable structure due to lack of docking rights", "owner_type": owner_type}

    except Exception as e:
        logger.exception(f"Failed to fetch sovereignty for system ID {system_id}: {e}")
        e_short = e.__class__.__name__
        e_detail = getattr(e, 'code', None) or getattr(e, 'status', None) or str(e)
        return {"owner_id": owner_id, "owner_name": f"Unresolvable sov, {e_short}{e_detail}", "owner_type": owner_type}

    # 3) Determine owner ID and type
    alliance_id = entry.get("alliance_id")
    faction_id = entry.get("faction_id")
    if alliance_id:  # Prefer alliance owners when present.
        owner_id = str(alliance_id)
        owner_type = "alliance"
    elif faction_id:  # Otherwise fall back to faction ownership.
        owner_id = str(faction_id)
        owner_type = "faction"
    else:
        return {"owner_id": "0", "owner_name": "Unclaimed", "owner_type": "unknown"}

    # 4) Resolve owner name
    try:
        owner_name = resolve_alliance_name(int(owner_id))
    except (TypeError, ValueError):
        owner_name = "Unresolvable owner"
        owner_id = "0"
        owner_type = "unknown"
    return {"owner_id": owner_id, "owner_name": owner_name, "owner_type": owner_type}





def get_users():
    """List the character names of every member-state user with a main set."""
    member_states = BigBrotherConfig.get_solo().bb_member_states.all()
    users = list(
        UserProfile.objects.filter(state__in=member_states)
        .exclude(main_character=None)
        .values_list("main_character__character_name", flat=True)
        .order_by("main_character__character_name")
    )
    return users

def get_user_profiles():
    """Return queryset of eligible user profiles with main characters eager-loaded."""
    member_states = BigBrotherConfig.get_solo().bb_member_states.all()
    users = (
        UserProfile.objects.filter(state__in=member_states)
        .exclude(main_character=None)
        .select_related("main_character", "user")  # optimization
        .order_by("main_character__character_name")
    )
    return users

def get_user_id(character_name):
    """Translate a main-character name into the owning Auth user id."""
    try:
        ownership = CharacterOwnership.objects.select_related('user').get(character__character_name=character_name)
        return ownership.user.id
    except CharacterOwnership.DoesNotExist:
        return None

def is_nullsec(system_id):
    try:
        sys = EveSolarSystem.objects.get(id=system_id)
        return sys.security_status <= 0.0
    except EveSolarSystem.DoesNotExist:
        return False

def is_player_structure(location_id):
    """
    Returns True if location_id likely corresponds to a player-owned structure
    (Citadel, Engineering Complex, Refinery) rather than an NPC station.
    Structure IDs are typically large (>= 1,000,000,000,000).
    """
    return location_id >= 1_000_000_000_000

def is_ship(type_id):
    return False # Placeholder logic will be inline or enhanced if needed.

def get_safe_entities():
    """
    Returns a set of safe entity IDs (whitelist, ignored, members).
    """
    from .models import BigBrotherConfig
    cfg = BigBrotherConfig.get_solo()

    ids = set()

    # Whitelists
    if cfg.whitelist_alliances:
        ids.update(int(x) for x in cfg.whitelist_alliances.split(',') if x.strip().isdigit())
    if cfg.whitelist_corporations:
        ids.update(int(x) for x in cfg.whitelist_corporations.split(',') if x.strip().isdigit())

    # Ignored
    if cfg.ignored_corporations:
        ids.update(int(x) for x in cfg.ignored_corporations.split(',') if x.strip().isdigit())

    # Members
    if cfg.member_corporations:
        ids.update(int(x) for x in cfg.member_corporations.split(',') if x.strip().isdigit())
    if cfg.member_alliances:
        ids.update(int(x) for x in cfg.member_alliances.split(',') if x.strip().isdigit())

    # Main corp/alliance
    if cfg.main_corporation_id:
        ids.add(cfg.main_corporation_id)
    if cfg.main_alliance_id:
        ids.add(cfg.main_alliance_id)

    return ids

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
