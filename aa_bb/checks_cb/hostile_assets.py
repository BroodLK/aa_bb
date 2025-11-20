"""
Corporate-level asset ownership checks.

These helpers inspect corp audits to find the systems where corp assets
live and highlight systems owned by alliances on the hostile list.
"""

from allianceauth.eveonline.models import EveCorporationInfo
from ..app_settings import get_system_owner
from ..models import BigBrotherConfig
from django.utils.html import format_html
from typing import List, Optional, Dict
import logging

logger = logging.getLogger(__name__)

try:
    from corptools.models import CorporationAudit, CorpAsset, EveLocation
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

def get_asset_locations(corp_id: int) -> Dict[int, Optional[str]]:
    """
    Return a dict mapping system IDs to their names (or None if unnamed)
    where the given corporation has one or more assets in space.
    """
    try:
        corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
        corp_audit = CorporationAudit.objects.get(corporation=corp_info)
    except CorporationAudit.DoesNotExist:
        return {}

    system_map: Dict[int, Optional[str]] = {}

    def add_system(system_obj):
        """Track the system when the asset resolves to a solar system."""
        if system_obj:  # Skip placeholder corp assets with missing solar system.
            key = getattr(system_obj, 'pk', None)
            system_map[key] = system_obj.name

    # All corp assets (exclude ones where location_flag is "solar_system")
    assets = CorpAsset.objects.select_related('location_name__system') \
                              .filter(corporation=corp_audit) \
                              .exclude(location_flag="solar_system")

    for asset in assets:
        loc = asset.location_name
        add_system(getattr(loc, 'system', None))

    sorted_items = sorted(
        system_map.items(),
        key=lambda kv: (kv[1] or "").lower()
    )
    return dict(sorted_items)

def get_corp_hostile_asset_locations(corp_id: int) -> Dict[str, str]:
    """
    Return {system name -> owner name} entries for hostile corp asset locations.

    Only systems that cannot be resolved or that belong to a hostile alliance
    are included in the response.
    """
    # get_asset_locations now returns Dict[int, Optional[str]]
    systems = get_asset_locations(corp_id)
    if not systems:  # No corp assets means nothing to audit.
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

        if not owner_info:  # Treat missing sovereignty as an unresolved owner.
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
        if oid in hostile_ids or "Unresolvable" in oname:  # Persist only hostile or unresolved sovereignty holders.
            hostile_map[display_name] = oname
            logger.info(f"Hostile asset system: {display_name} owned by {oname} ({oid})")

    return hostile_map


def render_assets(corp_id: int) -> Optional[str]:
    """
    Render an HTML table of systems where the corporation owns assets in space.

    The table mirrors the member-level view but operates on corp audits and
    highlights hostile sovereignty holders in red.
    """
    systems = get_asset_locations(corp_id)
    logger.info(f"corp id {corp_id}, systems {len(systems)}")
    if not systems:  # Short-circuit when no corp assets exist.
        return None

    # Parse hostile IDs into a set of ints
    hostile_str = BigBrotherConfig.get_solo().hostile_alliances or ""
    hostile_ids = {int(s) for s in hostile_str.split(",") if s.strip().isdigit()}
    #logger.debug(f"Hostile IDs for assets: {hostile_ids}")

    html = '<table class="table table-striped">'
    html += '<thead><tr><th>System</th><th>Owner</th></tr></thead><tbody>'

    for system_id, system_name in systems.items():
        # build the dict your get_system_owner() wants:
        owner_info = get_system_owner({
            "id":   system_id,
            "name": system_name or f"Unknown ({system_id})"
        })
        if owner_info:  # Only resolve sovereignty details when SDE returns something.
            raw_owner_id = owner_info.get("owner_id")
            if raw_owner_id:  # Convert IDs to ints when present.
                try:
                    oid = int(raw_owner_id)
                except (ValueError, TypeError):
                    oid = None
            else:
                oid = None

            if oid is not None:  # Positive ID enables a name/hostile lookup.
                oname = owner_info.get("owner_name") or f"ID {oid}"
                hostile = oid in hostile_ids or "Unresolvable" in oname
            else:  # Placeholder dash for missing owner IDs.
                oname = "—"
                hostile = False
        else:  # Unresolvable owner data falls back to dashes.
            oname = "—"
            hostile = False

        # ← THIS must be indented inside the loop!
        if hostile:  # Paint hostile ownership red for attention.
            row_tpl = '<tr><td>{}</td><td style="color: red;">{}</td></tr>'
        else:  # Neutral owners get default styling.
            row_tpl = '<tr><td>{}</td><td>{}</td></tr>'
        html += format_html(row_tpl, system_name, oname)

    html += "</tbody></table>"
    return html
