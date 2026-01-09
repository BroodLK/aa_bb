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
    """
    if not corptools_active() or CharacterAudit is None:
        return {}
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    system_map: Dict[int, dict] = {}

    def add_asset(system_obj, location_id, location_name, char_id, char_name, type_id, type_name):
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
                "assets": [],
            }

        system_map[key]["locations"][loc_key]["assets"].append({
            "char_id": char_id,
            "char_name": char_name,
            "type_id": type_id,
            "type_name": type_name,
        })

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
                location_type__in=["station", "structure", "other"],
            )
            .exclude(location_flag="solar_system")
        )

        for asset in assets:
            if (asset.location_flag or "").lower() == "assetsafety":
                continue

            loc = asset.location_name
            system_obj = getattr(loc, "system", None) if loc else None
            loc_name = resolve_location_name(asset.location_id) or f"Location {asset.location_id}"

            add_asset(
                system_obj, asset.location_id, loc_name,
                co.character.character_id, co.character.character_name,
                asset.type_name.type_id, asset.type_name.name
            )

    return system_map


def get_hostile_asset_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a mapping of system display name -> owner/asset summary string
    for systems where the user's characters have assets in space and the
    system is considered hostile under the unified processor logic.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return {}

    hostile_map: Dict[str, str] = {}

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"Unknown ({system_id})"

        system_hostile = False
        hostile_ships = set()
        hostile_chars = set()

        # We need the system owner info for the summary string
        owner_info = get_system_owner({"id": system_id, "name": display_name})
        oname = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"

        # Check each location in this system
        for loc_id, loc_data in data.get("locations", {}).items():
            for asset in loc_data.get("assets", []):
                # Use unified check for each asset
                if is_hostile_unified(
                    involved_ids=[asset["char_id"]],
                    location_id=loc_id,
                    system_id=system_id,
                    is_asset=True,
                    asset_type_id=asset["type_id"],
                    when=timezone.now()
                ):
                    system_hostile = True
                    hostile_chars.add(asset["char_name"])
                    if is_ship(asset["type_id"]):
                        hostile_ships.add(asset["type_name"])

        if not system_hostile:
            continue

        # Build the owner/detail string
        parts = [oname]
        rname = owner_info.get("region_name") if owner_info else None
        if rname and rname != "Unknown Region":
            parts.append(f"Region: {rname}")

        if hostile_ships:
            parts.append("Ships: " + ", ".join(sorted(hostile_ships)))

        if hostile_chars:
            parts.append("Chars: " + ", ".join(sorted(hostile_chars)))

        owner_summary = " | ".join(parts)
        hostile_map[display_name] = owner_summary
        logger.info(f"Hostile asset system: {display_name} owned by {oname}")

    return hostile_map



def render_assets(user_id: int) -> Optional[str]:
    """
    Returns an HTML table listing each system where the user's characters have assets,
    the system's sovereign owner, and highlights in red any asset considered hostile.
    """
    systems = get_asset_locations(user_id)
    if not systems:
        return None

    rows: List[Dict] = []

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
                char_name = asset["char_name"]
                if char_name not in char_assets:
                    char_assets[char_name] = {"ships": [], "is_hostile": False, "char_id": asset["char_id"]}

                # Check if this specific asset is hostile
                is_hostile = is_hostile_unified(
                    involved_ids=[asset["char_id"]],
                    location_id=loc_id,
                    system_id=system_id,
                    is_asset=True,
                    asset_type_id=asset["type_id"],
                    when=timezone.now()
                )

                if is_hostile:
                    char_assets[char_name]["is_hostile"] = True
                    if is_ship(asset["type_id"]):
                        char_assets[char_name]["ships"].append(asset["type_name"])

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
        return "<p>No hostile assets found.</p>"

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
    return html_output
