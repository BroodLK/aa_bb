"""
Identify where members keep assets in space and flag hostile owners.

The routines below are used both for the HTML renderings and faux-alerts
that can be sent when a user has assets in systems owned by enemies.
"""

from allianceauth.authentication.models import CharacterOwnership
from django.contrib.auth.models import User
from ..app_settings import get_system_owner
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
            key = getattr(system_obj, 'pk', None)
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
            system_map[key] = {
                "name": sys_name,
                "locations": {}
            }

        # Determine location name to use as key/label
        loc_key = location_id or 0
        if loc_key not in system_map[key]["locations"]:
            system_map[key]["locations"][loc_key] = {
                "name": location_name or f"Unknown Location {location_id}",
                "characters": {}
            }

        if char_name not in system_map[key]["locations"][loc_key]["characters"]:
            system_map[key]["locations"][loc_key]["characters"][char_name] = []

        if ship_name:
            system_map[key]["locations"][loc_key]["characters"][char_name].append(ship_name)

    # for each EVE character owned by this user
    for co in CharacterOwnership.objects.filter(user=user).select_related('character'):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # all their assets in space (exclude station containers, etc.)
        assets = CharacterAsset.objects.select_related(
            'location_name__system',
            'type_name__group__category'
        ).filter(character=char_audit).exclude(location_flag="solar_system")

        for asset in assets:
            loc = asset.location_name
            system_obj = getattr(loc, 'system', None) if loc else None
            # Fix: EveLocation uses location_name for the name string
            loc_name = getattr(loc, 'location_name',
                               f"Location {asset.location_id}") if loc else f"Location {asset.location_id}"

            ship_name = None
            try:
                if asset.type_name.group.category.name == "Ship":
                    ship_name = asset.type_name.name
            except AttributeError:
                pass

            add_asset(system_obj, asset.location_id, loc_name, co.character.character_name, ship_name)

    return system_map

def get_hostile_asset_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a dict of system display name → owning alliance name
    for systems where the user's characters have assets in space,
    including only those owned by hostile alliances or that are
    unresolvable.
    """
    # get_asset_locations now returns hierarchical structure
    systems = get_asset_locations(user_id)
    if not systems:
        return {}

    # parse hostile alliance IDs
    config = BigBrotherConfig.get_solo()
    hostile_str = config.hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}
    hostile_corp_str = config.hostile_corporations or ""
    hostile_corp_ids = {int(s) for s in hostile_corp_str.split(",") if s.strip().isdigit()}

    logger.debug(f"Hostile alliance IDs: {hostile_ids}")

    hostile_map: Dict[str, str] = {}

    # iterate system_id, data pairs
    for system_id, data in systems.items():
        system_name = data["name"]
        display_name = system_name or f"Unknown ({system_id})"

        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        if not owner_info:
            hostile_map[display_name] = "Unresolvable"
            continue

        try:
            oid = int(owner_info["owner_id"])
        except (ValueError, TypeError):
            oid = None

        oname = owner_info.get("owner_name") or (f"ID {oid}" if oid is not None else "Unresolvable")

        if oid in hostile_ids or oid in hostile_corp_ids or "Unresolvable" in oname:
            # Flatten ships for notification context
            all_ships = []
            for loc in data["locations"].values():
                for char_ships in loc["characters"].values():
                    all_ships.extend(char_ships)

            if all_ships:
                # Deduplicate ships nicely if needed, currently listing all instances
                oname += f" (Ships: {', '.join(all_ships)})"
            hostile_map[display_name] = oname
            logger.info(f"Hostile asset system: {display_name} owned by {oname} ({oid})")

    return hostile_map


def render_assets(user_id: int) -> Optional[str]:
    """
    Returns an HTML table listing each system where the user's characters have assets,
    the system's sovereign owner, and highlights in red any owner on the hostile list.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return None

    config = BigBrotherConfig.get_solo()
    hostile_str = config.hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}
    hostile_corp_str = config.hostile_corporations or ""
    hostile_corp_ids = {int(s) for s in hostile_corp_str.split(",") if s.strip().isdigit()}

    rows = []

    for system_id, data in systems.items():
        system_name = data["name"]
        display_name = system_name or f"Unknown ({system_id})"

        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        hostile = False
        oname = "—"

        if owner_info:
            try:
                oid = int(owner_info["owner_id"]) if owner_info["owner_id"] else None
            except (ValueError, TypeError):
                oid = None

            if oid is not None:
                oname = owner_info["owner_name"] or f"ID {oid}"
                hostile = oid in hostile_ids or oid in hostile_corp_ids or "Unresolvable" in oname

        # Iterate locations inside system
        for loc_id, loc_data in data["locations"].items():
            loc_name = loc_data["name"]

            # Iterate characters at location
            for char_name, ships in loc_data["characters"].items():
                ship_str = ", ".join(ships) if ships else ""
                rows.append({
                    "system": display_name,
                    "location": loc_name,
                    "character": char_name,
                    "owner": oname,
                    "hostile": hostile,
                    "ships": ship_str
                })

    # Sort rows: hostile first, then system name, then location
    rows.sort(key=lambda x: (not x["hostile"], x["system"], x["location"], x["character"]))

    html = '<table class="table table-striped table-hover stats">'
    html += ('<thead>'
             '  <tr>'
             '      <th style="width: 20%">System</th>'
             '      <th style="width: 20%">Station</th>'
             '      <th style="width: 20%">Character</th>'
             '      <th style="width: 20%">Owner</th>'
             '      <th style="width: 20%">Hostile Asset</th>'
             '  </tr>'
             '</thead>'
             '<tbody>')

    for row in rows:
        system_cell = row["system"]
        owner_cell = row["owner"]
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
            row["ships"]
        )

    html += "</tbody></table>"
    return html
