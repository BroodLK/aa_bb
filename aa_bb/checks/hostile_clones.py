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
    get_location_owner,
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

    audits = CharacterAudit.objects.filter(character__character_ownership__user_id=user_id).select_related("character")
    audit_ids = [a.pk for a in audits]
    char_map = {a.pk: a.character for a in audits}

    if not audit_ids:
        return {}

    system_map: Dict[int, dict] = {}
    _loc_sys_cache = {}
    _loc_name_cache = {}

    def add_location(system_obj, loc_id, char_id, char_name, implants=None, jump_clone_name=None):
        """Helper to safely extract system ID and name from various types."""
        sid = None
        sys_name = None

        if system_obj:
            sid = getattr(system_obj, "pk", None)
            sys_name = system_obj.name
        elif loc_id is not None:
            if loc_id in _loc_sys_cache:
                sid = _loc_sys_cache[loc_id]
            else:
                sid = resolve_location_system_id(loc_id)
                _loc_sys_cache[loc_id] = sid

            if sid:
                if sid in _loc_name_cache:
                    sys_name = _loc_name_cache[sid]
                else:
                    sys_name = resolve_location_name(sid)
                    _loc_name_cache[sid] = sys_name

        if not sid:
            return

        if sid not in system_map:
            system_map[sid] = {"name": sys_name, "locations": {}}

        loc_key = loc_id or 0
        if loc_key not in system_map[sid]["locations"]:
            if loc_key in _loc_name_cache:
                resolved_loc_name = _loc_name_cache[loc_key]
            else:
                resolved_loc_name = resolve_location_name(loc_id) or f"Location {loc_id}"
                _loc_name_cache[loc_key] = resolved_loc_name

            system_map[sid]["locations"][loc_key] = {"name": resolved_loc_name, "clones": []}

        system_map[sid]["locations"][loc_key]["clones"].append({
            "char_id": char_id,
            "char_name": char_name,
            "implants": implants or [],
            "jump_clone_name": jump_clone_name or "Jump Clone"
        })

    # Bulk fetch current locations
    active_locs = {}
    if CharacterLocation:
        active_locs = {
            cl.character_id: cl.current_location_id
            for cl in CharacterLocation.objects.filter(character_id__in=audit_ids)
        }

    # Bulk fetch home clones
    home_clones = Clone.objects.select_related("location_name__system").filter(character_id__in=audit_ids)
    for hc in home_clones:
        char = char_map.get(hc.character_id)
        if not char:
            continue
        status = "Home Station"
        if hc.location_id == active_locs.get(hc.character_id):
            status += " (Current Location)"
        add_location(getattr(hc.location_name, "system", None), hc.location_id, char.character_id, char.character_name, jump_clone_name=status)

    # Bulk fetch jump clones
    jump_clones = (
        JumpClone.objects.select_related("location_name__system")
        .prefetch_related("implant_set__type_name")
        .filter(character_id__in=audit_ids)
    )
    for jc in jump_clones:
        char = char_map.get(jc.character_id)
        if not char:
            continue
        implants = [i.type_name.name for i in jc.implant_set.all() if i.type_name]
        status = jc.name or "Jump Clone"
        if jc.location_id == active_locs.get(jc.character_id):
            status += " (Current Location)"
        add_location(getattr(jc.location_name, "system", None), jc.location_id, char.character_id, char.character_name, implants=implants, jump_clone_name=status)

    del _loc_sys_cache, _loc_name_cache, char_map, audit_ids, active_locs
    import gc
    gc.collect()

    return system_map


def get_hostile_clone_locations(user_id: int, cfg: BigBrotherConfig = None, safe_entities: set = None, entity_info_cache: dict = None) -> Dict[str, dict]:
    """
    Returns a dict of system display name -> structured hostile data
    for systems where this user has home or jump clones in space and the
    system is considered hostile under the unified processor logic.
    """
    systems = get_clones(user_id)
    if not systems:
        return {}

    hostile_map: Dict[str, dict] = {}
    if safe_entities is None:
        from ..app_settings import get_safe_entities
        safe_entities = get_safe_entities()
    if cfg is None:
        cfg = BigBrotherConfig.get_solo()
    _hostile_memo = {}

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"ID {system_id}"

        # System owner info for summary
        owner_info = get_system_owner({"id": system_id, "name": display_name})
        system_owner_name = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"
        region_name = owner_info.get("region_name", "Unknown Region") if owner_info else "Unknown Region"

        system_has_hostile = False
        records = []

        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]
            # Check if this is a citadel and get its owner
            location_owner_info = get_location_owner(loc_id)
            loc_owner = location_owner_info.get("owner_name", system_owner_name) if location_owner_info else system_owner_name

            for clone in loc_data.get("clones", []):
                # Memoize check results for character+location+system
                memo_key = (clone["char_id"], loc_id, system_id)
                if memo_key in _hostile_memo:
                    is_hostile = _hostile_memo[memo_key]
                else:
                    is_hostile = is_hostile_unified(
                        involved_ids=[clone["char_id"]],
                        location_id=loc_id,
                        system_id=system_id,
                        is_asset=True,
                        when=timezone.now(),
                        safe_entities=safe_entities,
                        entity_info_cache=entity_info_cache,
                        cfg=cfg
                    )
                    _hostile_memo[memo_key] = is_hostile

                if is_hostile:
                    system_has_hostile = True
                    records.append({
                        "char_name": clone['char_name'],
                        "location_name": loc_name,
                        "owner_name": loc_owner,
                        "clone_name": clone['jump_clone_name']
                    })

        if system_has_hostile:
            hostile_map[display_name] = {
                "owner": system_owner_name,
                "region": region_name,
                "records": records
            }

    # CRITICAL FIX: Clean up memoization cache
    del _hostile_memo
    import gc
    gc.collect()

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
    from ..app_settings import get_safe_entities
    safe_entities = get_safe_entities()
    _hostile_memo = {}

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"ID {system_id}"

        owner_info = get_system_owner({"id": system_id, "name": display_name})
        system_owner_name = owner_info.get("owner_name", "Unresolvable") if owner_info else "Unresolvable"
        region_name = owner_info.get("region_name", "Unknown Region") if owner_info else "Unknown Region"

        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]

            # Check if this is a citadel (player structure) and get its owner
            location_owner_info = get_location_owner(loc_id)
            if location_owner_info:
                # Use citadel owner for clones in citadels
                oname = location_owner_info.get("owner_name", system_owner_name)
            else:
                # Use system owner for NPC stations or space
                oname = system_owner_name

            for clone in loc_data.get("clones", []):
                char_name = clone["char_name"]

                # Memoize check results for character+location+system
                memo_key = (clone["char_id"], loc_id, system_id)
                if memo_key in _hostile_memo:
                    is_hostile = _hostile_memo[memo_key]
                else:
                    is_hostile = is_hostile_unified(
                        involved_ids=[clone["char_id"]],
                        location_id=loc_id,
                        system_id=system_id,
                        is_asset=True,
                        when=timezone.now(),
                        safe_entities=safe_entities
                    )
                    _hostile_memo[memo_key] = is_hostile

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
    rows.sort(key=lambda x: (not x["hostile"], x["system"], x["location"], x["character"]))

    html_parts = [
        '<table class="table table-striped table-hover stats">',
        "<thead>"
        "<tr>"
        '<th style="width: 15%">System</th>'
        '<th style="width: 20%">Station</th>'
        '<th style="width: 15%">Character</th>'
        '<th style="width: 10%">Clone Status</th>'
        '<th style="width: 15%">Implants</th>'
        '<th style="width: 15%">Owner</th>'
        '<th style="width: 10%">Region</th>'
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
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                row["system"],
                row["location"],
                row["character"],
                row["jump_clone"],
                row["implants_html"],
                owner_cell,
                row["region"],
            )
        )

    html_parts.append("</tbody></table>")

    # CRITICAL FIX: Clean up memoization cache
    del _hostile_memo
    import gc
    gc.collect()

    return "".join(html_parts)
