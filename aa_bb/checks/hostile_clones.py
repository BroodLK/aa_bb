# hostile_clones.py
"""
Clone location analysis helpers.

Similar to the hostile asset check, these routines find home/jump clones,
resolve who owns each system, and flag anything that sits in hostile space.
"""

from django.contrib.auth.models import User

from allianceauth.authentication.models import CharacterOwnership

from django.utils.html import format_html
from django.utils.safestring import mark_safe
from typing import List, Optional, Dict

from ..app_settings import (
    get_system_owner,
    is_nullsec,
    get_safe_entities,
    is_player_structure,
)
from ..models import BigBrotherConfig
import logging

logger = logging.getLogger(__name__)

try:
    from corptools.models import CharacterAudit, Clone, JumpClone, Implant
except ImportError:
    logger.error("Corptools not installed, clone checks will not work.")


def get_clones(user_id: int) -> Dict[int, Optional[str]]:
    """
    Return a dict mapping system IDs to their names (or None if unnamed)
    where this user has clones.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    system_map: Dict[int, Optional[str]] = {}

    def add_location(system_obj, loc_id):
        """Store system name/id for the clone location."""
        if system_obj:  # Clone located in a known system—store the friendly name.
            # use .pk for primary key, map to its name
            system_map[system_obj.pk] = system_obj.name
        elif loc_id is not None:  # Fallback when EveLocation missing but ID available.
            # fallback for unnamed systems
            system_map[loc_id] = None

    # iterate through all characters owned by the user
    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related(
                "location_name__system"
            ).get(character=char_audit)
            loc = home_clone.location_name
            add_location(getattr(loc, "system", None), home_clone.location_id)
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related(
            "location_name__system"
        ).filter(character=char_audit)
        for jc in jump_clones:
            loc = jc.location_name
            add_location(getattr(loc, "system", None), jc.location_id)

    # Optionally sort by name (None last) and return
    sorted_items = sorted(system_map.items(), key=lambda kv: (kv[1] or "").lower())
    return dict(sorted_items)


def get_hostile_clone_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a dict of system display name -> owner/clone summary string
    for systems where this user has home or jump clones in space and the
    system is considered hostile under the configured rules.

    The summary string includes:
      - the owning alliance/corp (or "Unresolvable"),
      - optional character names that have clones in that system.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    # Ensure corptools models are available (imported at module level)
    try:
        CharacterAudit  # type: ignore[name-defined]
        Clone           # type: ignore[name-defined]
        JumpClone       # type: ignore[name-defined]
    except NameError:
        logger.error("Corptools not installed, clone checks will not work.")
        return {}

    # Build a map of system_id -> name and system_id -> set of character names
    system_name_map: Dict[int, Optional[str]] = {}
    system_char_map: Dict[int, set] = {}

    def add_location(system_obj, loc_id, char_name: str) -> None:
        # Store system name/id and which character has a clone there.
        if system_obj:  # known system object
            sid = getattr(system_obj, "pk", None)
            if sid is None:
                return
            system_name_map[sid] = system_obj.name
        elif loc_id is not None:
            sid = loc_id
            system_name_map.setdefault(sid, None)
        else:
            return

        system_char_map.setdefault(sid, set()).add(char_name)

    # Walk all owned characters and their clones
    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        char_name = co.character.character_name

        try:
            char_audit = CharacterAudit.objects.get(  # type: ignore[name-defined]
                character=co.character
            )
        except CharacterAudit.DoesNotExist:  # type: ignore[attr-defined]
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related(  # type: ignore[name-defined]
                "location_name__system"
            ).get(character=char_audit)
            loc = home_clone.location_name
            add_location(getattr(loc, "system", None), home_clone.location_id, char_name)
        except Clone.DoesNotExist:  # type: ignore[attr-defined]
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related(  # type: ignore[name-defined]
            "location_name__system"
        ).filter(character=char_audit)
        for jc in jump_clones:
            loc = jc.location_name
            add_location(getattr(loc, "system", None), jc.location_id, char_name)

    if not system_name_map:
        return {}

    # Sort systems by name for stable output
    systems: Dict[int, Optional[str]] = dict(
        sorted(system_name_map.items(), key=lambda kv: (kv[1] or "").lower())
    )

    cfg = BigBrotherConfig.get_solo()

    hostile_str = cfg.hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}

    excluded_systems_str = cfg.excluded_systems or ""
    excluded_system_ids = {
        int(s) for s in excluded_systems_str.split(",") if s.strip().isdigit()
    }

    consider_nullsec = cfg.consider_nullsec_hostile
    safe_entities = get_safe_entities()

    hostile_map: Dict[str, str] = {}

    for system_id, system_name in systems.items():
        if system_id in excluded_system_ids:
            continue

        display_name = system_name or f"ID {system_id}"

        owner_info = get_system_owner(
            {
                "id": system_id,
                "name": display_name,
            }
        )

        nullsec_flag = consider_nullsec and is_nullsec(system_id)
        oname = "Unresolvable"
        oid: Optional[int] = None

        if not owner_info:
            # fully unresolvable, still worth flagging
            parts = [oname]
            char_list = sorted(system_char_map.get(system_id, set()))
            if char_list:
                parts.append("Chars: " + ", ".join(char_list))
            hostile_map[display_name] = " | ".join(parts)
            logger.info("Hostile clone (unresolvable): %s", display_name)
            continue

        try:
            oid = int(owner_info["owner_id"])
        except (ValueError, TypeError):
            oid = None
        oname = owner_info.get("owner_name") or (
            f"ID {oid}" if oid is not None else "Unresolvable"
        )

        if consider_nullsec and is_nullsec(system_id):
            if oid is None or oid not in safe_entities:
                nullsec_flag = True

        if (
            nullsec_flag
            or (oid in hostile_ids if oid is not None else False)
            or "Unresolvable" in oname
        ):
            parts = [oname]
            char_list = sorted(system_char_map.get(system_id, set()))
            if char_list:
                parts.append("Chars: " + ", ".join(char_list))
            summary = " | ".join(parts)
            hostile_map[display_name] = summary
            logger.info(
                "Hostile clone: %s owned by %s (%s)",
                display_name,
                summary,
                oid,
            )

    return hostile_map


def render_clones(user_id: int) -> Optional[str]:
    """
    Returns an HTML table of clones, coloring hostile ones red,
    and labeling & highlighting Unresolvable owners appropriately.
    Hostile if:
      - system owner alliance is in hostile_alliances / hostile_corporations, or
      - system is nullsec and consider_nullsec_hostile is enabled, or
      - in a hostile / NPC structure depending on config.
    Respects system & station whitelists.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return None

    clones_list: List[Dict] = []

    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related(
                "location_name__system"
            ).get(character=char_audit)
            loc = home_clone.location_name
            system_obj = getattr(loc, "system", None)
            if system_obj:
                sys_name = system_obj.name
                sys_id = system_obj.pk
            else:
                sys_name = None
                sys_id = home_clone.location_id

            clones_list.append(
                {
                    "character": co.character.character_name,
                    "id": sys_id,
                    "location_id": home_clone.location_id,
                    "name": sys_name,
                    "jump_clone": "Active Clone",
                    "implants": [],
                }
            )

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
            jump_name = jc.name
            system_obj = getattr(loc, "system", None)

            if system_obj:
                sys_name = system_obj.name
                sys_id = system_obj.pk
            else:
                sys_name = None
                sys_id = jc.location_id

            implants = [i.type_name.name for i in jc.implant_set.all() if i.type_name]

            clones_list.append(
                {
                    "character": co.character.character_name,
                    "id": sys_id,
                    "location_id": jc.location_id,
                    "name": sys_name,
                    "jump_clone": jump_name,
                    "implants": implants,
                }
            )

    if not clones_list:
        return None

    cfg = BigBrotherConfig.get_solo()
    hostile_alli_ids = {
        int(s) for s in (cfg.hostile_alliances or "").split(",") if s.strip().isdigit()
    }
    hostile_corp_ids = {
        int(s) for s in (cfg.hostile_corporations or "").split(",") if s.strip().isdigit()
    }

    excluded_system_ids = {
        int(s) for s in (cfg.excluded_systems or "").split(",") if s.strip().isdigit()
    }
    excluded_station_ids = {
        int(s) for s in (cfg.excluded_stations or "").split(",") if s.strip().isdigit()
    }

    consider_nullsec = cfg.consider_nullsec_hostile
    consider_structures = cfg.consider_all_structures_hostile
    consider_npc = getattr(cfg, "consider_npc_stations_hostile", False)

    safe_entities = get_safe_entities()

    rows: List[Dict] = []

    # We’ll still sort by character/name initially to keep things stable,
    # but final sort will put hostile rows on top.
    clones_list.sort(key=lambda x: (x["character"], (x["name"] or "").lower()))

    for clone in clones_list:
        system_id = clone.get("id")
        system_name = clone.get("name")
        loc_id = clone.get("location_id")

        # Build a friendlier system display:
        # - use known system name when available
        # - otherwise distinguish between system id and pure location id
        if system_name:
            display_name = system_name
        else:
            if system_id and system_id == loc_id:
                # Only have a station/structure id
                display_name = f"Location ID {loc_id}"
            elif system_id:
                display_name = f"System ID {system_id}"
            elif loc_id:
                display_name = f"Location ID {loc_id}"
            else:
                display_name = "Unknown"

        # System whitelist only applies when we actually know the system id
        if system_id and system_id in excluded_system_ids:
            continue

        # For ownership checks, fall back to location_id when system_id is missing – this
        # preserves your current "Unresolvable structure due to lack of docking rights"
        # messaging for player structures.
        owner_key = system_id or loc_id

        owner_info = None
        if owner_key:
            owner_info = get_system_owner(
                {
                    "id": owner_key,
                    "name": display_name,
                }
            )

        oid: Optional[int] = None
        oname = "Unresolvable"
        base_hostile = False
        unresolvable = False

        if owner_info:
            try:
                oid = int(owner_info.get("owner_id")) if owner_info.get("owner_id") else None
            except (ValueError, TypeError):
                oid = None

            if oid is not None:
                oname = owner_info.get("owner_name") or f"ID {oid}"
                base_hostile = (
                    (oid in hostile_alli_ids)
                    or (oid in hostile_corp_ids)
                    or ("Unresolvable" in oname)
                )
            else:
                # No usable owner id – treat as unresolvable-ish
                oname = owner_info.get("owner_name") or "Unresolvable"
                base_hostile = True
                unresolvable = "Unresolvable" in oname
        else:
            # No owner info at all → treat as unresolvable/hostile
            base_hostile = True
            unresolvable = True
            oname = "Unresolvable"

        # Nullsec flag – hostile unless sov owner is on a safe list
        nullsec_flag = False
        if consider_nullsec and system_id:
            if is_nullsec(system_id):
                if oid is None or oid not in safe_entities:
                    nullsec_flag = True

        # Structure / NPC flags – use the actual clone location (station/structure)
        struct_flag = False
        npc_flag = False
        if (consider_structures or consider_npc) and loc_id and loc_id not in excluded_station_ids:
            is_struct = is_player_structure(loc_id)

            if consider_structures and is_struct:
                if oid is None or oid not in safe_entities:
                    struct_flag = True

            if consider_npc and not is_struct:
                npc_flag = True

        hostile = base_hostile or nullsec_flag or struct_flag or npc_flag

        rows.append(
            {
                "character": clone["character"],
                "system": display_name,
                "jump_clone": clone["jump_clone"] or "",
                "implants_html": mark_safe("<br>".join(clone["implants"])),
                "owner": oname,
                "hostile": hostile,
                "unresolvable": unresolvable,
            }
        )

    if not rows:
        return '<p>No clones found.</p>'

    # === NEW: put hostile rows at the top ===
    rows.sort(key=lambda r: (not r["hostile"], r["character"], r["system"]))

    html_parts = [
        '<table class="table table-striped table-hover stats">',
        "<thead>"
        "<tr>"
        "<th>Character</th>"
        "<th>System</th>"
        "<th>Clone Status</th>"
        "<th>Implants</th>"
        "<th>Owner</th>"
        "</tr>"
        "</thead>"
        "<tbody>",
    ]

    for row in rows:
        if row["hostile"]:
            row_tpl = (
                "<tr><td>{}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td class=\"text-danger\">{}</td></tr>"
            )
        elif row["unresolvable"]:
            row_tpl = (
                "<tr><td>{}</td><td>{}</td><td>{}</td>"
                "<td>{}</td><td class=\"text-warning\"><em>{}</em></td></tr>"
            )
        else:
            row_tpl = "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>"

        html_parts.append(
            format_html(
                row_tpl,
                row["character"],
                row["system"],
                row["jump_clone"],
                row["implants_html"],
                row["owner"],
            )
        )

    html_parts.append("</tbody></table>")
    return "".join(html_parts)
