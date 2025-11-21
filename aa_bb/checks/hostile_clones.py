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

from ..app_settings import get_system_owner
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
    Only includes locations owned by hostile alliances or unresolvable.
    """
    systems = get_clones(user_id)  # Dict[int, Optional[str]]
    if not systems:
        return {}

    hostile_str = BigBrotherConfig.get_solo().hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}

    hostile_map: Dict[str, str] = {}

    # systems: key = system_id (int), value = system_name (str or None)
    for system_id, system_name in systems.items():
        display_name = system_name or f"ID {system_id}"

        # build the dict get_system_owner expects
        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        if not owner_info:  # Unresolved sovereignty, mark as unresolvable entry.
            # fully unresolvable
            hostile_map[display_name] = "Unresolvable"
            #logger.debug(f"No owner info for clone in {display_name}; marked Unresolvable")
            continue

        oid = int(owner_info["owner_id"])
        oname = owner_info["owner_name"] or f"ID {oid}"

        # include only hostile or unresolvable owners
        if oid in hostile_ids or "Unresolvable" in oname:  # Alert when clone sits in hostile or unknown space.
            hostile_map[display_name] = oname
            logger.info(f"Hostile clone: {display_name} owned by {oname} ({oid})")

    return hostile_map



def render_clones(user_id: int) -> Optional[str]:
    """
    Returns an HTML table of clones, coloring hostile ones red,
    and labeling & highlighting Unresolvable owners appropriately.
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
                'implants': [],  # Home clone implants not available via JumpClone Implant model
            })
        except Clone.DoesNotExist:
            pass

        # Jump clones
        jump_clones = JumpClone.objects.select_related('location_name__system')\
            .prefetch_related('implant_set__type_name')\
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

    if not clones_list:  # No clones to report.
        return None

    hostile_str = BigBrotherConfig.get_solo().hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}

    html = [
        '<table class="table table-striped table-hover stats">',
        '<thead><tr><th>Character</th><th>System</th><th>Clone Status</th><th>Implants</th><th>Owner</th></tr></thead><tbody>'
    ]

    # Sort by character then system name
    clones_list.sort(key=lambda x: (x['character'], (x['name'] or "").lower()))

    for clone in clones_list:
        system_id = clone['id']
        system_name = clone['name']

        # build the dict get_system_owner expects
        owner_info = get_system_owner({
            "id":   system_id,
            "name": system_name
        })

        if owner_info:
            oid = int(owner_info["owner_id"])
            oname = owner_info["owner_name"] or f"ID {oid}"
            hostile = oid in hostile_ids or "Unresolvable" in oname
            unresolvable = False
        else:
            oname = "Unresolvable"  # No sovereignty info returned.
            hostile = False
            unresolvable = True

        if hostile:  # Highlight hostile entries in red.
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="text-danger">{}</td></tr>'
        elif unresolvable:  # Use warning styling for unknown owners.
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td class="text-warning"><em>{}</em></td></tr>'
        else:  # Neutral owners get normal formatting.
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
