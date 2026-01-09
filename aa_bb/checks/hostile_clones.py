# hostile_clones.py
"""
Clone location analysis helpers.

Similar to the hostile asset check, these routines find home/jump clones,
resolve who owns each system, and flag anything that sits in hostile space
using the unified processor.
"""

from django.contrib.auth.models import User
import html
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from django.utils.html import format_html
from django.utils.safestring import mark_safe
from typing import List, Optional, Dict

from ..app_settings import (
    get_system_owner,
    is_nullsec,
    get_safe_entities,
    is_player_structure,
    resolve_location_name,
    resolve_location_system_id,
    is_highsec,
    is_lowsec,
    corptools_active,
    is_hostile_unified,
)
from django.utils import timezone
from ..models import BigBrotherConfig

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import CharacterAudit, Clone, JumpClone, Implant, CharacterLocation
    else:
        CharacterAudit = None
        Clone = None
        JumpClone = None
        Implant = None
        CharacterLocation = None
except ImportError:
    CharacterAudit = None
    Clone = None
    JumpClone = None
    Implant = None
    CharacterLocation = None


def get_clones(user_id: int) -> Dict[int, dict]:
    """
    Return a dict mapping system IDs to a dict containing their name and a list of locations
    where this user has clones.
    """
    if not corptools_active() or CharacterAudit is None:
        return {}
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    system_map: Dict[int, dict] = {}

    def add_location(system_obj, loc_id, char_id, char_name, implants=None, jump_clone_name=None):
        """Helper to safely extract system ID and name from various types."""
        sid = None
        sys_name = None

        if system_obj:
            sid = getattr(system_obj, "pk", None)
            sys_name = system_obj.name
        elif loc_id is not None:
            sid = resolve_location_system_id(loc_id)
            if sid:
                sys_name = resolve_location_name(sid)

        if not sid:
            return

        if sid not in system_map:
            system_map[sid] = {"name": sys_name, "locations": {}}

        loc_key = loc_id or 0
        if loc_key not in system_map[sid]["locations"]:
            system_map[sid]["locations"][loc_key] = {"name": resolve_location_name(loc_id) or f"Location {loc_id}", "clones": []}

        system_map[sid]["locations"][loc_key]["clones"].append({
            "char_id": char_id,
            "char_name": char_name,
            "implants": implants or [],
            "jump_clone_name": jump_clone_name or "Jump Clone"
        })

    # iterate through all characters owned by the user
    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        char_name = co.character.character_name
        char_id = co.character.character_id
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Get current active location
        active_location_id = None
        if CharacterLocation:
            try:
                char_loc = CharacterLocation.objects.get(character=char_audit)
                active_location_id = char_loc.current_location_id
            except CharacterLocation.DoesNotExist:
                pass

        # Home clone
        try:
            home_clone = Clone.objects.select_related(
                "location_name__system"
            ).get(character=char_audit)
            loc = home_clone.location_name
            status = "Home Station"
            if home_clone.location_id == active_location_id:
                status += " (Active)"
            add_location(getattr(loc, "system", None), home_clone.location_id, char_id, char_name, jump_clone_name=status)
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = (
            JumpClone.objects.select_related("location_name__system")
            .prefetch_related("implant_set__type_name")
            .filter(character=char_audit)
        )
        for jc in jump_clones:
            loc = jc.location_name
            implants = [i.type_name.name for i in jc.implant_set.all() if i.type_name]
            status = jc.name or "Jump Clone"
            if jc.location_id == active_location_id:
                status += " (Active)"
            add_location(getattr(loc, "system", None), jc.location_id, char_id, char_name, implants=implants, jump_clone_name=status)

    return system_map


def get_hostile_clone_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a dict of system display name -> owner/clone summary string
    for systems where this user has home or jump clones in space and the
    system is considered hostile under the unified processor logic.
    """
    systems = get_clones(user_id)
    if not systems:
        return {}

    hostile_map: Dict[str, str] = {}

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"ID {system_id}"

        system_hostile = False
        hostile_chars = set()

        # System owner info for summary
        owner_info = get_system_owner({"id": system_id, "name": display_name})
        oname = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"

        for loc_id, loc_data in data.get("locations", {}).items():
            for clone in loc_data.get("clones", []):
                if is_hostile_unified(
                    involved_ids=[clone["char_id"]],
                    location_id=loc_id,
                    system_id=system_id,
                    when=timezone.now()
                ):
                    system_hostile = True
                    hostile_chars.add(f"{clone['char_name']} [{clone['jump_clone_name']}]")

        if not system_hostile:
            continue

        # Build the detail string
        parts = [oname]
        rname = owner_info.get("region_name") if owner_info else None
        if rname and rname != "Unknown Region":
            parts.append(f"Region: {rname}")

        if hostile_chars:
            parts.append("Chars: " + ", ".join(sorted(hostile_chars)))

        owner_summary = " | ".join(parts)
        hostile_map[display_name] = owner_summary

    return hostile_map


def render_clones(user_id: int) -> str:
    """
    Render an HTML table of locations where the user has clones,
    highlighting hostile locations using the unified processor.
    """
    systems = get_clones(user_id)
    if not systems:
        return '<p>No clones found.</p>'

    rows: List[Dict] = []

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"ID {system_id}"

        owner_info = get_system_owner({"id": system_id, "name": display_name})
        oname = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"
        region_name = owner_info.get("region_name", "Unknown Region") if owner_info else "Unknown Region"

        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]

            for clone in loc_data.get("clones", []):
                char_name = clone["char_name"]

                is_hostile = is_hostile_unified(
                    involved_ids=[clone["char_id"]],
                    location_id=loc_id,
                    system_id=system_id,
                    when=timezone.now()
                )

                rows.append({
                    "system": display_name,
                    "location": loc_name,
                    "character": char_name,
                    "owner": oname,
                    "region": region_name,
                    "hostile": is_hostile,
                    "jump_clone": clone["jump_clone_name"],
                    "implants_html": mark_safe("<br>".join(clone["implants"])),
                })

    if not rows:
        return "<p>No clones found.</p>"

    # Sort: hostile first, then system, location, character
    rows.sort(key=lambda x: (not x["hostile"], x["character"], x["system"]))

    html_parts = [
        '<table class="table table-striped table-hover stats">',
        "<thead>"
        "<tr>"
        "<th>Character</th>"
        "<th>System</th>"
        "<th>Clone Status</th>"
        "<th>Implants</th>"
        "<th>Owner</th>"
        "<th>Region</th>"
        "</tr>"
        "</thead>"
        "<tbody>",
    ]

    for row in rows:
        owner_cell = row["owner"]
        if row["hostile"]:
            owner_cell = mark_safe(f'<span class="text-danger">{owner_cell}</span>')

        html_parts.append(
            format_html(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                row["character"],
                row["system"],
                row["jump_clone"],
                row["implants_html"],
                owner_cell,
                row["region"],
            )
        )

    html_parts.append("</tbody></table>")
    return "".join(html_parts)
