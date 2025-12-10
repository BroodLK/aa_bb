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
            # Fallback if system object isn't available but we have a location ID
            # For now, assuming we only care if we can map to a system,
            # but to support "Unknown Location" groups properly we can use negative ID or similar if needed.
            # Current logic requires key to be set.
            # Use location_id as key if system unavailable?
            # But return type says Dict[int, dict] where int is system ID.
            # If we have no system, we can't check sovereignty easily.
            pass

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
            # Fix: EveLocation uses location_name for the name string
            loc_name = (
                getattr(loc, "location_name", f"Location {asset.location_id}")
                if loc
                else f"Location {asset.location_id}"
            )

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

        owner_info = get_system_owner({"id": system_id, "name": display_name})

        oid: Optional[int] = None
        oname = "Unresolvable"
        base_hostile = False

        if owner_info:
            try:
                oid = int(owner_info["owner_id"])
            except (ValueError, TypeError):
                oid = None

            oname = owner_info.get("owner_name") or (
                f"ID {oid}" if oid is not None else "Unresolvable"
            )
            base_hostile = (
                (oid in hostile_ids)
                or (oid in hostile_corp_ids)
                or ("Unresolvable" in oname)
            )
        else:
            # No sov info – keep "Unresolvable" behaviour
            base_hostile = True

        # Config: treat all nullsec as hostile
        nullsec_flag = consider_nullsec and is_nullsec(system_id)

        # Config: structural / station-type based hostility
        struct_flag = False
        npc_flag = False

        if consider_structures or consider_npc:
            for loc_id, loc_data in data.get("locations", {}).items():
                if not loc_id or loc_id in excluded_station_ids:
                    continue

                is_struct = is_player_structure(loc_id)

                # Player-owned structures not on any safe list
                if consider_structures and is_struct:
                    if oid is None or oid not in safe_entities:
                        struct_flag = True

                # NPC stations (non player-owned), if opted in
                if consider_npc and not is_struct:
                    npc_flag = True

                if struct_flag or npc_flag:
                    break

        system_hostile = base_hostile or nullsec_flag or struct_flag or npc_flag
        if not system_hostile:
            continue

        # Flatten ships and characters for context + ship-only filter
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

        owner_info = get_system_owner({
            "id": system_id,
            "name": display_name
        })

        oid: Optional[int] = None
        oname = "—"
        base_hostile = False

        if owner_info:
            try:
                oid = int(owner_info["owner_id"]) if owner_info["owner_id"] else None
            except (ValueError, TypeError):
                oid = None

            if oid is not None:
                oname = owner_info["owner_name"] or f"ID {oid}"
                base_hostile = (
                    (oid in hostile_ids)
                    or (oid in hostile_corp_ids)
                    or ("Unresolvable" in oname)
                )
        else:
            oname = "Unresolvable"
            base_hostile = True

        nullsec_flag = False
        if consider_nullsec and is_nullsec(system_id):
            if oid is None or oid not in safe_entities:
                nullsec_flag = True

        struct_flag = False
        npc_flag = False
        if consider_structures or consider_npc:
            for loc_id, loc_data in data["locations"].items():
                if not loc_id or loc_id in excluded_station_ids:
                    continue

                is_struct = is_player_structure(loc_id)

                if consider_structures and is_struct:
                    if oid is None or oid not in safe_entities:
                        struct_flag = True

                if consider_npc and not is_struct:
                    npc_flag = True

                if struct_flag or npc_flag:
                    break

        system_hostile = base_hostile or nullsec_flag or struct_flag or npc_flag

        # Iterate locations inside system
        for loc_id, loc_data in data["locations"].items():
            # Station/structure whitelist
            if loc_id in excluded_station_ids:
                continue

            loc_name = loc_data["name"]

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
                        "hostile": system_hostile,
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
        '      <th style="width: 20%">System</th>'
        '      <th style="width: 20%">Station</th>'
        '      <th style="width: 20%">Character</th>'
        '      <th style="width: 20%">Owner</th>'
        '      <th style="width: 20%">Hostile Asset</th>'
        '  </tr>'
        '</thead>'
        '<tbody>'
    )

    for row in rows:
        system_cell = row["system"]
        owner_cell = row["owner"]
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
            '   </tr>',
            system_cell,
            row["location"],
            row["character"],
            owner_cell,
            hostile_ship,
        )

    html += '</tbody></table>'
    return html
