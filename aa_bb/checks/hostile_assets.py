"""
Identify where members keep assets in space and flag hostile owners.

The routines below are used both for the HTML renderings and faux-alerts
that can be sent when a user has assets in systems owned by enemies.
"""

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger
from django.contrib.auth.models import User
from ..app_settings import (
    get_system_owner,
    is_nullsec,
    is_player_structure,
    get_safe_entities,
    resolve_location_name,
    resolve_location_system_id,
    is_highsec,
    is_lowsec,
    corptools_active,
    is_hostile_unified,
    is_ship,
)
from django.utils import timezone
from ..models import BigBrotherConfig
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from typing import List, Optional, Dict

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import CharacterAudit, CharacterAsset, EveLocation
    else:
        CharacterAudit = None
        CharacterAsset = None
        EveLocation = None
except ImportError:
    CharacterAudit = None
    CharacterAsset = None
    EveLocation = None


def _parse_id_list(value: Optional[str]) -> set[int]:
    if not value:
        return set()
    return {int(x) for x in value.split(",") if x.strip().isdigit()}


def get_asset_locations(user_id: int) -> Dict[int, dict]:
    """
    Return a dict mapping system IDs to a dict containing their name and a list of locations
    (stations/structures) where any of the given user's characters has one or more assets.

    OPTIMIZED: Limits asset processing and cleans up memory.
    """
    if not corptools_active() or CharacterAudit is None:
        return {}

    # Optimized fetching of all assets for all user characters in one go
    audits = CharacterAudit.objects.filter(character__character_ownership__user_id=user_id).select_related("character")
    audit_ids = [a.pk for a in audits]
    char_map = {a.pk: a.character for a in audits}

    if not audit_ids:
        return {}

    assets = (
        CharacterAsset.objects.filter(character_id__in=audit_ids)
        .select_related("location_name__system", "type_name")
        .exclude(location_flag__iexact="solar_system")[:5000]  # Limit to prevent memory explosion
    )

    system_map: Dict[int, dict] = {}
    _loc_sys_cache = {}
    _loc_name_cache = {}
    processed_combos = set()

    max_combos = 10000  # Safety limit

    for asset in assets:
        if (asset.location_flag or "").lower() == "assetsafety":
            continue

        if not asset.type_name:
            continue

        char = char_map.get(asset.character_id)
        if not char:
            continue

        # Unique combo check: (character, location, type)
        # Prevents redundant processing of multiple stacks of the same item
        combo = (char.character_id, asset.location_id, asset.type_name.type_id)
        if combo in processed_combos:
            continue

        if len(processed_combos) >= max_combos:
            logger.warning(f"[hostile_assets] User {user_id} hit {max_combos} asset combo limit, stopping processing")
            break

        processed_combos.add(combo)

        loc = asset.location_name
        system_obj = getattr(loc, "system", None) if loc else None
        location_id = asset.location_id

        key = None
        sys_name = None

        if system_obj:
            key = system_obj.pk
            sys_name = system_obj.name
        elif location_id:
            if location_id in _loc_sys_cache:
                key = _loc_sys_cache[location_id]
            else:
                key = resolve_location_system_id(location_id)
                _loc_sys_cache[location_id] = key

            if key:
                if key in _loc_name_cache:
                    sys_name = _loc_name_cache[key]
                else:
                    sys_name = resolve_location_name(key)
                    _loc_name_cache[key] = sys_name

        if not key:
            continue

        if key not in system_map:
            system_map[key] = {"name": sys_name, "locations": {}}

        loc_key = location_id or 0
        if loc_key not in system_map[key]["locations"]:
            # Determine location name
            loc_name = None
            if loc:
                loc_name = loc.location_name
            if not loc_name:
                if loc_key in _loc_name_cache:
                    loc_name = _loc_name_cache[loc_key]
                else:
                    loc_name = resolve_location_name(loc_key) or f"Unknown Location {loc_key}"
                    _loc_name_cache[loc_key] = loc_name

            system_map[key]["locations"][loc_key] = {
                "name": loc_name,
                "assets": [],
            }

        system_map[key]["locations"][loc_key]["assets"].append({
            "char_id": char.character_id,
            "char_name": char.character_name,
            "type_id": asset.type_name.type_id,
            "type_name": asset.type_name.name,
        })

    del processed_combos, _loc_sys_cache, _loc_name_cache, char_map, audit_ids
    import gc
    gc.collect()

    return system_map


def get_hostile_asset_locations(user_id: int, cfg: BigBrotherConfig = None, safe_entities: set = None, entity_info_cache: dict = None) -> Dict[str, dict]:
    """
    Returns a mapping of system display name -> structured hostile data
    for systems where the user's characters have assets in space and the
    system is considered hostile under the unified processor logic.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return {}

    hostile_map: Dict[str, dict] = {}
    if safe_entities is None:
        from ..app_settings import get_safe_entities
        safe_entities = get_safe_entities()
    if cfg is None:
        cfg = BigBrotherConfig.get_solo()
    ships_only = cfg.hostile_assets_ships_only
    _hostile_memo = {}

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"Unknown ({system_id})"

        # We need the system owner info for the summary
        owner_info = get_system_owner({"id": system_id, "name": display_name})
        oname = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"
        rname = owner_info.get("region_name", "Unknown Region") if owner_info else "Unknown Region"

        system_has_hostile = False
        # structured data: list of records
        records = []

        # Check each location in this system
        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]

            # Map of char_name -> set of ships for this specific location
            char_ships_at_loc = {}
            location_has_hostile = False

            for asset in loc_data.get("assets", []):
                char_id = asset["char_id"]
                char_name = asset["char_name"]
                type_id = asset["type_id"]

                # Memoize check results for character+location (ignoring type for the base check)
                memo_key = (char_id, loc_id, system_id)
                if memo_key in _hostile_memo:
                    is_hostile_at_loc = _hostile_memo[memo_key]
                else:
                    # Check base hostility for character at this location
                    is_hostile_at_loc = is_hostile_unified(
                        involved_ids=[char_id],
                        location_id=loc_id,
                        system_id=system_id,
                        is_asset=True,
                        asset_type_id=None,
                        when=timezone.now(),
                        safe_entities=safe_entities,
                        entity_info_cache=entity_info_cache,
                        cfg=cfg
                    )
                    _hostile_memo[memo_key] = is_hostile_at_loc

                is_hostile_asset = False
                if is_hostile_at_loc:
                    if not ships_only:
                        is_hostile_asset = True

                    if is_ship(type_id):
                        char_ships_at_loc.setdefault(char_name, set()).add(asset["type_name"])
                        if ships_only:
                            is_hostile_asset = True
                    else:
                        # Ensure character is recorded even if no ships (e.g. just modules/items)
                        char_ships_at_loc.setdefault(char_name, set())

                if is_hostile_asset:
                    system_has_hostile = True
                    location_has_hostile = True

            if location_has_hostile:
                for cname, ships in char_ships_at_loc.items():
                    records.append({
                        "char_name": cname,
                        "location_name": loc_name,
                        "ships": sorted(list(ships))
                    })

        if system_has_hostile:
            hostile_map[display_name] = {
                "owner": oname,
                "region": rname,
                "records": records
            }
            logger.info(f"Hostile asset system: {display_name} owned by {oname}")

    # CRITICAL FIX: Clean up memoization cache
    del _hostile_memo
    import gc
    gc.collect()

    return hostile_map



def render_assets(user_id: int) -> Optional[str]:
    """
    Returns an HTML table listing each system where the user's characters have assets,
    the system's sovereign owner, and highlights in red any asset considered hostile.
    """
    try:
        systems = get_asset_locations(user_id)
        if not systems:
            return None

        rows: List[Dict] = []
        from ..app_settings import get_safe_entities
        safe_entities = get_safe_entities()
        cfg = BigBrotherConfig.get_solo()
        ships_only = cfg.hostile_assets_ships_only
        _hostile_memo = {}

        for system_id, data in systems.items():
            system_name = data.get("name")
            display_name = system_name or f"Unknown ({system_id})"

            # Base system owner info for the table
            owner_info = get_system_owner({"id": system_id, "name": display_name})
            oname = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"
            region_name = owner_info.get("region_name", "Unknown Region") if owner_info else "Unknown Region"

            # Iterate locations inside system
            for loc_id, loc_data in data.get("locations", {}).items():
                loc_name = loc_data["name"]

                # Check each asset group (char/type combo)
                # Actually we can group by char for rendering
                char_assets = {}
                for asset in loc_data.get("assets", []):
                    char_id = asset["char_id"]
                    char_name = asset["char_name"]
                    type_id = asset["type_id"]

                    if char_name not in char_assets:
                        char_assets[char_name] = {"ships": [], "is_hostile": False, "char_id": char_id}

                    # Memoize check results for character+location (ignoring type for base check)
                    memo_key = (char_id, loc_id, system_id)
                    if memo_key in _hostile_memo:
                        is_hostile_at_loc = _hostile_memo[memo_key]
                    else:
                        # Check base hostility for character at this location
                        is_hostile_at_loc = is_hostile_unified(
                            involved_ids=[char_id],
                            location_id=loc_id,
                            system_id=system_id,
                            is_asset=True,
                            asset_type_id=None,
                            when=timezone.now(),
                            safe_entities=safe_entities
                        )
                        _hostile_memo[memo_key] = is_hostile_at_loc

                    if is_hostile_at_loc:
                        if not ships_only:
                            char_assets[char_name]["is_hostile"] = True

                        if is_ship(type_id):
                            char_assets[char_name]["ships"].append(asset["type_name"])
                            if ships_only:
                                char_assets[char_name]["is_hostile"] = True

                for char_name, cdata in char_assets.items():
                    ship_str = ", ".join(sorted(cdata["ships"])) if cdata["ships"] else ""
                    rows.append({
                        "system": display_name,
                        "location": loc_name,
                        "character": char_name,
                        "owner": oname,
                        "region": region_name,
                        "hostile": cdata["is_hostile"],
                        "ships": ship_str,
                    })

        if not rows:
            return '<table class="table stats"><tbody><tr><td class="text-center">No hostile assets found.</td></tr></tbody></table>'

        # Sort rows: hostile first, then by system, location, character
        rows.sort(key=lambda x: (not x["hostile"], x["system"], x["location"], x["character"]))

        html_output = '<table class="table table-striped table-hover stats">'
        html_output += (
            '<thead>'
            '  <tr>'
            '      <th style="width: 15%">System</th>'
            '      <th style="width: 20%">Station</th>'
            '      <th style="width: 15%">Character</th>'
            '      <th style="width: 15%">Owner</th>'
            '      <th style="width: 15%">Region</th>'
            '      <th style="width: 20%">Hostile Asset</th>'
            '  </tr>'
            '</thead>'
            '<tbody>'
        )

        for row in rows:
            system_cell = row["system"]
            owner_cell = row["owner"]
            region_cell = row["region"]
            hostile_ship = row["ships"] if row["hostile"] else ""

            if row["hostile"]:
                owner_cell = mark_safe(f'<span class="text-danger">{owner_cell}</span>')

            html_output += format_html(
                '   <tr>'
                '       <td>{}</td>'
                '       <td>{}</td>'
                '       <td>{}</td>'
                '       <td>{}</td>'
                '       <td>{}</td>'
                '       <td>{}</td>'
                '   </tr>',
                system_cell,
                row["location"],
                row["character"],
                owner_cell,
                region_cell,
                hostile_ship,
            )

        html_output += '</tbody></table>'

        # CRITICAL FIX: Clean up memoization cache
        del _hostile_memo
        import gc
        gc.collect()

        return html_output
    except Exception as e:
        logger.exception(f"Error rendering assets for user {user_id}")
        return f"<p class='text-danger'>Error rendering assets: {str(e)}</p>"
