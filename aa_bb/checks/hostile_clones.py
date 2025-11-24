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

from ..app_settings import get_system_owner, is_nullsec, get_safe_entities
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
    for co in CharacterOwnership.objects.filter(user=user).select_related('character'):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related('location_name__system').get(character=char_audit)
            loc = home_clone.location_name
            add_location(getattr(loc, 'system', None), home_clone.location_id)
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related('location_name__system').filter(character=char_audit)
        for jc in jump_clones:
            loc = jc.location_name
            add_location(getattr(loc, 'system', None), jc.location_id)

    # Optionally sort by name (None last) and return
    sorted_items = sorted(
        system_map.items(),
        key=lambda kv: (kv[1] or "").lower()
    )
    return dict(sorted_items)



def get_hostile_clone_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a dict of system display name → owning alliance name,
    including 'Unresolvable' where owner info is unavailable.
    Includes locations that are:
      - in a hostile alliance, or
      - in nullsec when consider_nullsec_hostile is enabled, or
      - unresolvable.
    Respects system whitelists.
    """
    systems = get_clones(user_id)  # Dict[int, Optional[str]]
    if not systems:
        return {}

    cfg = BigBrotherConfig.get_solo()
    hostile_str = cfg.hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}

    excluded_systems_str = cfg.excluded_systems or ""
    excluded_system_ids = {int(s) for s in excluded_systems_str.split(",") if s.strip().isdigit()}

    consider_nullsec = cfg.consider_nullsec_hostile
    safe_entities = get_safe_entities()

    hostile_map: Dict[str, str] = {}

    for system_id, system_name in systems.items():
        if system_id in excluded_system_ids:
            continue

        display_name = system_name or f"ID {system_id}"

        owner_info = get_system_owner({
            "id": system_id,
            "name": display_name
        })

        nullsec_flag = consider_nullsec and is_nullsec(system_id)

        if not owner_info:
            # fully unresolvable, still worth flagging
            oname = "Unresolvable"
            hostile_map[display_name] = oname
            logger.info(f"Hostile clone (unresolvable): {display_name}")
            continue

        try:
            oid = int(owner_info["owner_id"])
        except (ValueError, TypeError):
            oid = None
            oname = owner_info.get("owner_name") or (f"ID {oid}" if oid is not None else "Unresolvable")
            # Nullsec is hostile unless sov owner is “safe”
            nullsec_flag = False
        if consider_nullsec and is_nullsec(system_id):
            if oid is None or oid not in safe_entities:
                nullsec_flag = True
        if nullsec_flag or (oid in hostile_ids if oid is not None else False) or "Unresolvable" in oname:
            hostile_map[display_name] = oname
            logger.info(f"Hostile clone: {display_name} owned by {oname} ({oid})")

    return hostile_map



def render_clones(user_id: int) -> Optional[str]:
    """
    Returns an HTML table of clones, coloring hostile ones red,
    and labeling & highlighting Unresolvable owners appropriately.
    Hostile if:
      - system owner alliance is in hostile_alliances, or
      - system is nullsec and consider_nullsec_hostile is enabled.
    Respects system whitelists.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return None

    clones_list = []

    for co in CharacterOwnership.objects.filter(user=user).select_related('character'):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # Home clone
        try:
            home_clone = Clone.objects.select_related('location_name__system').get(character=char_audit)
            loc = home_clone.location_name
            system_obj = getattr(loc, 'system', None)
            if system_obj:
                sys_name = system_obj.name
                sys_id = system_obj.pk
            else:
                sys_name = None
                sys_id = home_clone.location_id

            clones_list.append({
                'character': co.character.character_name,
                'id': sys_id,
                'name': sys_name,
                'jump_clone': "Home Station",
                'implants': [],
            })
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related('location_name__system') \
            .prefetch_related('implant_set__type_name') \
            .filter(character=char_audit)

        for jc in jump_clones:
            loc = jc.location_name
            jump_name = jc.name
            system_obj = getattr(loc, 'system', None)
            if system_obj:
                sys_name = system_obj.name
                sys_id = system_obj.pk
            else:
                sys_name = None
                sys_id = jc.location_id

            implants = [i.type_name.name for i in jc.implant_set.all() if i.type_name]

            clones_list.append({
                'character': co.character.character_name,
                'id': sys_id,
                'name': sys_name,
                'jump_clone': jump_name,
                'implants': implants,
            })

    if not clones_list:
        return None

    cfg = BigBrotherConfig.get_solo()
    hostile_str = cfg.hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}

    excluded_systems_str = cfg.excluded_systems or ""
    excluded_system_ids = {int(s) for s in excluded_systems_str.split(",") if s.strip().isdigit()}

    consider_nullsec = cfg.consider_nullsec_hostile
    safe_entities = get_safe_entities()

    html = [
        '<table class="table table-striped table-hover stats">',
        '<thead><tr><th>Character</th><th>System</th><th>Clone Status</th><th>Implants</th><th>Owner</th></tr></thead><tbody>'
    ]

    clones_list.sort(key=lambda x: (x['character'], (x['name'] or "").lower()))

    for clone in clones_list:
        system_id = clone['id']
        system_name = clone['name']

        if system_id in excluded_system_ids:
            continue

        owner_info = get_system_owner({
            "id": system_id,
            "name": system_name
        })

        nullsec_flag = consider_nullsec and is_nullsec(system_id)

        owner_info = get_system_owner({
            "id": system_id,
            "name": system_name
        })

        oid = None
        oname = "Unresolvable"
        unresolvable = False
        hostile = False

        if owner_info:
            try:
                oid = int(owner_info["owner_id"])
            except (ValueError, TypeError):
                oid = None

            oname = owner_info.get("owner_name") or (f"ID {oid}" if oid is not None else "Unresolvable")

            base_hostile = (oid in hostile_ids) or ("Unresolvable" in oname)
        else:
            base_hostile = True
            oname = "Unresolvable"

        # Nullsec logic
        if consider_nullsec and is_nullsec(system_id):
            if oid is None or oid not in safe_entities:
                nullsec_flag = True
            else:
                nullsec_flag = False
        else:
            nullsec_flag = False

        hostile = base_hostile or nullsec_flag

        if hostile:
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="text-danger">{}</td></tr>'
        elif unresolvable:
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="text-warning"><em>{}</em></td></tr>'
        else:
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'

        html.append(
            format_html(
                row_tpl,
                clone['character'],
                system_name or f"ID {system_id}",
                clone['jump_clone'] or "",
                mark_safe("<br>".join(clone['implants'])),
                oname
            )
        )

    html.append('</tbody></table>')
    return "".join(html)
