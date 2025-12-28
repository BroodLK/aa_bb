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
    resolve_location_name,
    resolve_location_system_id,
    is_highsec,
    is_lowsec,
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
            system_map[loc_id] = resolve_location_name(loc_id)

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

    # Build a map of system_id -> { "name": name, "locations": { loc_id: set(char_names) } }
    system_map: Dict[int, dict] = {}

    def add_location(system_obj, loc_id, char_name: str) -> None:
        # Store system name/id and which character has a clone there.
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
            system_map[sid]["locations"][loc_key] = set()

        system_map[sid]["locations"][loc_key].add(char_name)

    # Walk all owned characters and their clones
    for co in CharacterOwnership.objects.filter(user=user).select_related("character"):
        char_name = co.character.character_name

        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related("location_name__system").get(character=char_audit)
            loc = home_clone.location_name
            add_location(getattr(loc, "system", None), home_clone.location_id, char_name)
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related("location_name__system").filter(character=char_audit)
        for jc in jump_clones:
            loc = jc.location_name
            add_location(getattr(loc, "system", None), jc.location_id, char_name)

    if not system_map:
        return {}

    cfg = BigBrotherConfig.get_solo()

    hostile_ids = {int(s) for s in (cfg.hostile_alliances or "").split(",") if s.strip().isdigit()}
    hostile_corp_ids = {int(s) for s in (cfg.hostile_corporations or "").split(",") if s.strip().isdigit()}

    excluded_system_ids = {int(s) for s in (cfg.excluded_systems or "").split(",") if s.strip().isdigit()}
    excluded_station_ids = {int(s) for s in (cfg.excluded_stations or "").split(",") if s.strip().isdigit()}

    consider_nullsec = cfg.consider_nullsec_hostile
    consider_structures = cfg.consider_all_structures_hostile
    consider_npc = getattr(cfg, "consider_npc_stations_hostile", False)

    safe_entities = get_safe_entities()

    hostile_map: Dict[str, str] = {}

    # Sort systems by name for stable output
    sorted_systems = sorted(system_map.items(), key=lambda x: (x[1]["name"] or "").lower())

    for system_id, data in sorted_systems:
        if system_id in excluded_system_ids:
            continue

        if cfg.exclude_high_sec and is_highsec(system_id):
            continue
        if cfg.exclude_low_sec and is_lowsec(system_id):
            continue

        system_name = data.get("name")
        display_name = system_name or f"ID {system_id}"

        # Base system hostility
        owner_info = get_system_owner({"id": system_id, "name": display_name})
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

        system_hostile = False
        # Check each location in this system
        for loc_id in data.get("locations", {}):
            if not loc_id or loc_id in excluded_station_ids:
                continue

            loc_hostile = False
            is_struct = is_player_structure(loc_id)

            # Get location owner specifically
            loc_owner_info = get_system_owner({"id": loc_id})
            l_oid = None
            l_oname = "Unresolvable"
            if loc_owner_info:
                try:
                    l_oid = int(loc_owner_info["owner_id"]) if loc_owner_info["owner_id"] else None
                except (ValueError, TypeError):
                    pass
                l_oname = loc_owner_info.get("owner_name") or (f"ID {l_oid}" if l_oid else "Unresolvable")

            if is_struct:
                # Friendly structure overrides system hostility
                if l_oid and l_oid in safe_entities:
                    continue

                if l_oid and (l_oid in hostile_ids or l_oid in hostile_corp_ids):
                    loc_hostile = True
                elif "Unresolvable" in l_oname:
                    loc_hostile = True
                elif consider_structures:
                    loc_hostile = True
            else:
                # NPC Station
                if consider_npc:
                    loc_hostile = True
                elif base_hostile or nullsec_flag:
                    loc_hostile = True

            if loc_hostile:
                system_hostile = True
                break

        if system_hostile:
            parts = [oname]
            rname = owner_info.get("region_name")
            if rname and rname != "Unknown Region":
                parts.append(f"Region: {rname}")

            char_names = set()
            for loc_chars in data.get("locations", {}).values():
                char_names.update(loc_chars)

            if char_names:
                parts.append("Chars: " + ", ".join(sorted(char_names)))

            summary = " | ".join(parts)
            hostile_map[display_name] = summary
            logger.info("Hostile clone: %s owned by %s", display_name, summary)

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
            home_clone = Clone.objects.select_related("location_name__system").get(character=char_audit)
            loc = home_clone.location_name
            system_obj = getattr(loc, "system", None)

            sys_id = None
            sys_name = None
            if system_obj:
                sys_id = system_obj.pk
                sys_name = system_obj.name
            else:
                sys_id = resolve_location_system_id(home_clone.location_id)
                if sys_id:
                    sys_name = resolve_location_name(sys_id)

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

            sys_id = None
            sys_name = None
            if system_obj:
                sys_id = system_obj.pk
                sys_name = system_obj.name
            else:
                sys_id = resolve_location_system_id(jc.location_id)
                if sys_id:
                    sys_name = resolve_location_name(sys_id)

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
    hostile_ids = {int(s) for s in (cfg.hostile_alliances or "").split(",") if s.strip().isdigit()}
    hostile_corp_ids = {int(s) for s in (cfg.hostile_corporations or "").split(",") if s.strip().isdigit()}

    excluded_system_ids = {int(s) for s in (cfg.excluded_systems or "").split(",") if s.strip().isdigit()}
    excluded_station_ids = {int(s) for s in (cfg.excluded_stations or "").split(",") if s.strip().isdigit()}

    consider_nullsec = cfg.consider_nullsec_hostile
    consider_structures = cfg.consider_all_structures_hostile
    consider_npc = getattr(cfg, "consider_npc_stations_hostile", False)

    safe_entities = get_safe_entities()

    rows: List[Dict] = []

    # Final sort will put hostile rows on top.
    clones_list.sort(key=lambda x: (x["character"], (x["name"] or "").lower()))

    for clone in clones_list:
        system_id = clone.get("id")
        system_name = clone.get("name")
        loc_id = clone.get("location_id")

        if system_name:
            display_name = system_name
        elif system_id:
            display_name = resolve_location_name(system_id) or f"System ID {system_id}"
        elif loc_id:
            display_name = resolve_location_name(loc_id) or f"Location ID {loc_id}"
        else:
            display_name = "Unknown"

        if system_id and system_id in excluded_system_ids:
            continue

        # Manual hostility check
        owner_info = get_system_owner({"id": system_id or loc_id, "name": display_name})
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
        if system_id and consider_nullsec and is_nullsec(system_id):
            if oid is None or oid not in safe_entities:
                nullsec_flag = True

        hostile = False
        is_struct = is_player_structure(loc_id)

        # Get location owner specifically
        loc_owner_info = get_system_owner({"id": loc_id})
        l_oid = None
        l_oname = "Unresolvable"
        if loc_owner_info:
            try:
                l_oid = int(loc_owner_info["owner_id"]) if loc_owner_info["owner_id"] else None
            except (ValueError, TypeError):
                pass
            l_oname = loc_owner_info.get("owner_name") or (f"ID {l_oid}" if l_oid else "Unresolvable")

        if is_struct:
            # Friendly structure overrides system hostility
            if l_oid and l_oid in safe_entities:
                hostile = False
            elif l_oid and (l_oid in hostile_ids or l_oid in hostile_corp_ids):
                hostile = True
            elif "Unresolvable" in l_oname:
                hostile = True
            elif consider_structures:
                hostile = True
        else:
            # NPC Station
            if consider_npc:
                hostile = True
            elif base_hostile or nullsec_flag:
                hostile = True

        unresolvable = "Unresolvable" in l_oname

        rows.append(
            {
                "character": clone["character"],
                "system": display_name,
                "jump_clone": clone["jump_clone"] or "",
                "implants_html": mark_safe("<br>".join(clone["implants"])),
                "owner": oname,
                "region": owner_info.get("region_name") if owner_info else "Unknown Region",
                "hostile": hostile,
                "unresolvable": unresolvable,
            }
        )

    if not rows:
        return '<p>No clones found.</p>'

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
        "<th>Region</th>"
        "</tr>"
        "</thead>"
        "<tbody>",
    ]

    for row in rows:
        region = row.get("region", "Unknown Region")
        owner_cell = row["owner"]
        if row["hostile"]:
            owner_cell = mark_safe(f'<span class="text-danger">{owner_cell}</span>')
        elif row["unresolvable"]:
            owner_cell = mark_safe(f'<span class="text-warning"><em>{owner_cell}</em></span>')

        html_parts.append(
            format_html(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                row["character"],
                row["system"],
                row["jump_clone"],
                row["implants_html"],
                owner_cell,
                region,
            )
        )

    html_parts.append("</tbody></table>")
    return "".join(html_parts)
