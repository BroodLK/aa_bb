"""
Identify where members keep assets in space and flag hostile owners.

The routines below are used both for the HTML renderings and faux-alerts
that can be sent when a user has assets in systems owned by enemies.
"""

from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth.models import User
from ..app_settings import (
    get_system_owner,
    is_nullsec,
    is_player_structure,
    get_safe_entities,
    resolve_location_name,
    resolve_location_system_id,
    get_hostile_state,
)
from ..models import BigBrotherConfig
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)

try:
    from corptools.models import CharacterAudit, CharacterAsset, EveLocation
except ImportError:
    logger.error("Corptools not installed, asset checks will not work.")


def _parse_id_list(value: Optional[str]) -> set[int]:
    if not value:
        return set()
    return {int(x) for x in value.split(",") if x.strip().isdigit()}


def get_asset_locations(user_id: int) -> Dict[int, dict]:
    """
    Return a dict mapping system IDs to a dict containing their name and a list of locations
    (stations/structures) where any of the given user's characters has one or more assets.
    Structure:
    {
        system_id: {
            "name": system_name,
            "locations": {
                location_id: {
                    "name": location_name,
                    "characters": {
                        char_name: [ship_names...]
                    }
                }
            }
        }
    }
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    system_map: Dict[int, dict] = {}

    def add_asset(system_obj, location_id, location_name, char_name, ship_name=None):
        """Store the asset details organized by system and location."""
        key = None
        sys_name = None

        if system_obj:
            key = getattr(system_obj, "pk", None)
            sys_name = system_obj.name
        elif location_id:
            # Attempt to resolve system name
            key = resolve_location_system_id(location_id)
            if key:
                sys_name = resolve_location_name(key)

        if not key:
            return

        if key not in system_map:
            system_map[key] = {"name": sys_name, "locations": {}}

        # Determine location name to use as key/label
        loc_key = location_id or 0
        if loc_key not in system_map[key]["locations"]:
            system_map[key]["locations"][loc_key] = {
                "name": location_name or f"Unknown Location {location_id}",
                "characters": {},
            }

        if char_name not in system_map[key]["locations"][loc_key]["characters"]:
            system_map[key]["locations"][loc_key]["characters"][char_name] = []

        if ship_name:
            system_map[key]["locations"][loc_key]["characters"][char_name].append(
                ship_name
            )

    # for each EVE character owned by this user
    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # all their assets in space (exclude station containers, etc.)
        assets = (
            CharacterAsset.objects.select_related(
                "location_name__system",
                "type_name__group__category",
            )
            .filter(
                character=char_audit,
                location_type="station",
            )
            .exclude(location_flag="solar_system")
        )

        for asset in assets:
            if (asset.location_flag or "").lower() == "assetsafety":
                continue

            loc = asset.location_name
            system_obj = getattr(loc, "system", None) if loc else None
            loc_name = resolve_location_name(asset.location_id) or f"Location {asset.location_id}"

            ship_name = None
            try:
                if asset.type_name.group.category.name == "Ship":
                    ship_name = asset.type_name.name
            except AttributeError:
                pass

            add_asset(
                system_obj, asset.location_id, loc_name, co.character.character_name, ship_name
            )

    return system_map


def get_hostile_asset_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a mapping of system display name -> owner/asset summary string
    for systems where the user's characters have assets in space and the
    system is considered hostile under the configured rules.

    The summary string includes:
      - the owning alliance/corp (or "Unresolvable"),
      - optional ship names present in that system,
      - optional character names that have assets there.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return {}

    cfg = BigBrotherConfig.get_solo()

    hostile_ids = _parse_id_list(cfg.hostile_alliances or "")
    hostile_corp_ids = _parse_id_list(cfg.hostile_corporations or "")

    excluded_system_ids = _parse_id_list(cfg.excluded_systems or "")
    excluded_station_ids = _parse_id_list(cfg.excluded_stations or "")

    consider_nullsec = cfg.consider_nullsec_hostile
    consider_structures = cfg.consider_all_structures_hostile
    consider_npc = getattr(cfg, "consider_npc_stations_hostile", False)
    ships_only = getattr(cfg, "hostile_assets_ships_only", False)

    safe_entities = get_safe_entities()

    logger.debug("Hostile alliance IDs: %s", hostile_ids)
    logger.debug("Hostile corporation IDs: %s", hostile_corp_ids)

    hostile_map: Dict[str, str] = {}

    for system_id, data in systems.items():
        # System whitelist – never mark these as hostile
        if system_id in excluded_system_ids:
            continue

        system_name = data.get("name")
        display_name = system_name or f"Unknown ({system_id})"

        # Base system hostility
        system_hostile = get_hostile_state(system_id, 'solar_system')

        if not system_hostile and (consider_structures or consider_npc):
            for loc_id, loc_data in data.get("locations", {}).items():
                if not loc_id or loc_id in excluded_station_ids:
                    continue

                is_struct = is_player_structure(loc_id)
                if get_hostile_state(loc_id, 'structure' if is_struct else 'station'):
                    system_hostile = True
                    break

        if not system_hostile:
            continue

        owner_info = get_system_owner({"id": system_id, "name": display_name})
        oname = owner_info.get("owner_name") or "Unresolvable"
        oid = owner_info.get("owner_id")
        all_ships: list[str] = []
        char_names = set()

        for loc in data.get("locations", {}).values():
            for char_name, char_ships in loc.get("characters", {}).items():
                char_names.add(char_name)
                all_ships.extend(char_ships)

        if ships_only and not all_ships:
            # Config says: ignore systems where we only have non-ship assets
            continue

        # Build the owner/detail string
        parts = [oname]
        rname = owner_info.get("region_name")
        if rname and rname != "Unknown Region":
            parts.append(f"Region: {rname}")

        if all_ships:
            parts.append("Ships: " + ", ".join(sorted(all_ships)))

        if char_names:
            parts.append("Chars: " + ", ".join(sorted(char_names)))

        owner_summary = " | ".join(parts)

        hostile_map[display_name] = owner_summary
        logger.info(
            "Hostile asset system: %s owned by %s (%s)",
            display_name,
            owner_summary,
            oid,
        )

    return hostile_map



def render_assets(user_id: int) -> Optional[str]:
    """
    Returns an HTML table listing each system where the user's characters have assets,
    the system's sovereign owner, and highlights in red any owner on the hostile list,
    respecting nullsec / structure / NPC / whitelist / ship-only settings.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return None

    cfg = BigBrotherConfig.get_solo()

    hostile_ids = _parse_id_list(cfg.hostile_alliances or "")
    hostile_corp_ids = _parse_id_list(cfg.hostile_corporations or "")

    excluded_system_ids = _parse_id_list(cfg.excluded_systems or "")
    excluded_station_ids = _parse_id_list(cfg.excluded_stations or "")

    consider_nullsec = cfg.consider_nullsec_hostile
    consider_structures = cfg.consider_all_structures_hostile
    consider_npc = getattr(cfg, "consider_npc_stations_hostile", False)
    ships_only = getattr(cfg, "hostile_assets_ships_only", False)

    safe_entities = get_safe_entities()

    rows: List[Dict] = []

    for system_id, data in systems.items():
        # System whitelist – skip entirely
        if system_id in excluded_system_ids:
            continue

        system_name = data["name"]
        display_name = system_name or f"Unknown ({system_id})"

        # Base system hostility
        system_hostile = get_hostile_state(system_id, 'solar_system')

        # Iterate locations inside system
        for loc_id, loc_data in data["locations"].items():
            # Station/structure whitelist
            if loc_id in excluded_station_ids:
                continue

            loc_hostile = system_hostile
            is_struct = is_player_structure(loc_id)
            if not loc_hostile and (consider_structures or consider_npc):
                if get_hostile_state(loc_id, 'structure' if is_struct else 'station'):
                    loc_hostile = True

            loc_name = loc_data["name"]
            owner_info = get_system_owner({
                "id": system_id,
                "name": display_name
            })
            oname = owner_info.get("owner_name") or "—"

            for char_name, ships in loc_data["characters"].items():
                # Optionally ignore non-ship assets entirely
                if ships_only and not ships:
                    continue

                ship_str = ", ".join(ships) if ships else ""
                rows.append(
                    {
                        "system": display_name,
                        "location": loc_name,
                        "character": char_name,
                        "owner": oname,
                        "region": owner_info.get("region_name") if owner_info else "Unknown Region",
                        "hostile": loc_hostile,
                        "ships": ship_str,
                    }
                )

    if not rows:
        return "<p>No hostile assets found.</p>"

    rows.sort(
        key=lambda x: (
            not x["hostile"],
            x["system"],
            x["location"],
            x["character"],
        )
    )

    html = '<table class="table table-striped table-hover stats">'
    html += (
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
        region_cell = row.get("region", "Unknown Region")
        hostile_ship = row["ships"] if row["hostile"] else ""
        if row["hostile"]:
            owner_cell = mark_safe(f'<span class="text-danger">{owner_cell}</span>')

        html += format_html(
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

    html += '</tbody></table>'
    return html
