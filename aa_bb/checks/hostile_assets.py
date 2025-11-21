"""
Identify where members keep assets in space and flag hostile owners.

The routines below are used both for the HTML renderings and faux-alerts
that can be sent when a user has assets in systems owned by enemies.
"""

from django.contrib.auth.models import User
from allianceauth.authentication.models import CharacterOwnership
from ..app_settings import get_system_owner
from ..models import BigBrotherConfig
from django.utils.html import format_html
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)

try:
    from corptools.models import CharacterAudit, CharacterAsset, EveLocation
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

def get_asset_locations(user_id: int) -> Dict[int, Optional[str]]:
    """
    Return a dict mapping system IDs to their names (or None if unnamed)
    where any of the given user's characters has one or more assets in space.
    """
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {}

    system_map: Dict[int, Optional[str]] = {}

    def add_system(system_obj):
        """Store the system pk/name for any asset located in a solar system."""
        if system_obj:  # Only record systems when a valid EveLocation is present.
            key = getattr(system_obj, 'pk', None)
            system_map[key] = system_obj.name

    # for each EVE character owned by this user
    for co in CharacterOwnership.objects.filter(user=user).select_related('character'):
        try:
            char_audit = CharacterAudit.objects.get(character=co.character)
        except CharacterAudit.DoesNotExist:
            continue

        # all their assets in space (exclude station containers, etc.)
        assets = CharacterAsset.objects.select_related('location_name__system') \
                                       .filter(character=char_audit) \
                                       .exclude(location_flag="solar_system")
        for asset in assets:
            loc = asset.location_name
            add_system(getattr(loc, 'system', None))

    # sort by system name (None treated as empty string)
    sorted_items = sorted(
        system_map.items(),
        key=lambda kv: (kv[1] or "").lower()
    )

    return dict(sorted_items)

def get_hostile_asset_locations(user_id: int) -> Dict[str, str]:
    """
    Returns a dict of system display name → owning alliance name
    for systems where the user's characters have assets in space,
    including only those owned by hostile alliances or that are
    unresolvable.
    """
    # get_asset_locations now returns Dict[int, Optional[str]]
    systems = get_asset_locations(user_id)
    if not systems:  # No assets in space → nothing to flag.
        return {}

    # parse hostile alliance IDs
    hostile_str = BigBrotherConfig.get_solo().hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}
    logger.debug(f"Hostile alliance IDs: {hostile_ids}")

    hostile_map: Dict[str, str] = {}

    # iterate system_id, system_name pairs
    for system_id, system_name in systems.items():
        display_name = system_name or f"Unknown ({system_id})"

        # build the dict that get_system_owner expects
        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        if not owner_info:  # Treat missing owner info as unresolved.
            # treat fully missing owner info as unresolvable
            hostile_map[display_name] = "Unresolvable"
            #logger.debug(f"No ownership info for assets in {display_name}; marked Unresolvable")
            continue

        # attempt to parse owner_id
        try:
            oid = int(owner_info["owner_id"])
        except (ValueError, TypeError):
            oid = None

        oname = owner_info.get("owner_name") or (f"ID {oid}" if oid is not None else "Unresolvable")

        # include only hostile or unresolvable owners
        if oid in hostile_ids or "Unresolvable" in oname:  # Only include entries that match hostile criteria.
            hostile_map[display_name] = oname
            logger.info(f"Hostile asset system: {display_name} owned by {oname} ({oid})")

    return hostile_map


def render_assets(user_id: int) -> Optional[str]:
    """
    Returns an HTML table listing each system where the user's characters have assets,
    the system's sovereign owner, and highlights in red any owner on the hostile list.
    """
    systems = get_asset_locations(user_id)
    if not systems:  # Nothing to render when no assets in space.
        return None

    # Parse hostile IDs into a set of ints
    hostile_str = BigBrotherConfig.get_solo().hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}
    #logger.debug(f"Hostile IDs for assets: {hostile_ids}")

    html = '<table class="table table-striped table-hover stats">'
    html += '<thead><tr><th>System</th><th>Owner</th></tr></thead><tbody>'

    for system_id, system_name in systems.items():
        # build the dict your get_system_owner() wants:
        owner_info = get_system_owner({
            "id":   system_id,
            "name": system_name or f"Unknown ({system_id})"
        })
        if owner_info:
            try:
                # owner_id might be '' or None
                oid = int(owner_info["owner_id"]) if owner_info["owner_id"] else None
            except (ValueError, TypeError):
                oid = None

            if oid is not None:  # Found an alliance owner record; compare against hostile set.
                oname = owner_info["owner_name"] or f"ID {oid}"
                hostile = oid in hostile_ids or "Unresolvable" in oname
            else:
                # owner_id missing/empty -> neutral entry
                oname = "—"
                hostile = False
        else:
            # No ownership data returned at all
            oname = "—"
            hostile = False

        # ← THIS must be indented inside the loop!
        row_tpl = (
            '<tr><td>{}</td><td style="color: red;">{}</td></tr>'
            if hostile  # Highlight hostile ownership in red.
            else '<tr><td>{}</td><td>{}</td></tr>'
        )
        html += format_html(row_tpl, system_name, oname)

    html += "</tbody></table>"
    return html
