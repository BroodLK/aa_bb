"""
Corporate-level asset ownership checks.

These helpers inspect corp audits to find the systems where corp assets
live and highlight systems owned by alliances on the hostile list.
"""

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger
from ..app_settings import get_system_owner, resolve_location_name, resolve_location_system_id, get_hostile_state, corptools_active, is_hostile_unified
from ..models import BigBrotherConfig
from django.utils.html import format_html
from typing import List, Optional, Dict

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import CorporationAudit, CorpAsset, EveLocation
    else:
        CorporationAudit = None
        CorpAsset = None
        EveLocation = None
except ImportError:
    CorporationAudit = None
    CorpAsset = None
    EveLocation = None

def get_asset_locations(corp_id: int) -> Dict[int, Optional[str]]:
    """
    Return a dict mapping system IDs to their names (or None if unnamed)
    where the given corporation has one or more assets in space.
    """
    if not corptools_active() or CorporationAudit is None:
        return {}
    try:
        corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
        corp_audit = CorporationAudit.objects.get(corporation=corp_info)
    except CorporationAudit.DoesNotExist:
        return {}

    system_map: Dict[int, Optional[str]] = {}

    def add_system(system_obj, loc_id=None):
        """Track the system when the asset resolves to a solar system."""
        if system_obj:  # Skip placeholder corp assets with missing solar system.
            key = getattr(system_obj, 'pk', None)
            system_map[key] = system_obj.name
        elif loc_id:
            sid = resolve_location_system_id(loc_id)
            if sid:
                system_map[sid] = resolve_location_name(sid)

    # All corp assets (exclude ones where location_flag is "assetsafety")
    assets = CorpAsset.objects.select_related('location_name__system') \
                              .filter(
                                  corporation=corp_audit,
                                  location_type__in=["station", "other"]
                              )

    for asset in assets:
        loc = asset.location_name
        add_system(getattr(loc, 'system', None), getattr(loc, 'id', None))

    sorted_items = sorted(
        system_map.items(),
        key=lambda kv: (kv[1] or "").lower()
    )
    return dict(sorted_items)

def get_corp_hostile_asset_locations(corp_id: int) -> Dict[str, str]:
    """
    Return {system name -> owner name} entries for hostile corp asset locations.
    Uses the unified processor logic.
    """
    systems = get_asset_locations(corp_id)
    if not systems:
        return {}

    hostile_map: Dict[str, str] = {}

    for system_id, system_name in systems.items():
        display_name = system_name or f"Unknown ({system_id})"

        # Check hostility using unified processor
        if is_hostile_unified(
            involved_ids=[corp_id],
            system_id=system_id,
            is_asset=True
        ):
            owner_info = get_system_owner({
                "id":   system_id,
                "name": display_name
            })

            oname = owner_info.get("owner_name") or "Unresolvable"
            rname = owner_info.get("region_name")

            summary = oname
            if rname and rname != "Unknown Region":
                summary = f"{oname} | Region: {rname}"
            hostile_map[display_name] = summary
            logger.info(f"Hostile corp asset system: {display_name} owned by {summary}")

    return hostile_map


def render_assets(corp_id: int) -> Optional[str]:
    """
    Render an HTML table of systems where the corporation owns assets in space.
    Highlights hostile sovereignty holders in red using the unified processor.
    """
    systems = get_asset_locations(corp_id)
    if not systems:
        return None

    html_output = '<table class="table table-striped">'
    html_output += '<thead><tr><th>System</th><th>Owner</th><th>Region</th></tr></thead><tbody>'

    for system_id, system_name in systems.items():
        display_name = system_name or f"Unknown ({system_id})"
        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        oname = owner_info.get("owner_name") or "—"
        rname = owner_info.get("region_name") or "—"

        # Check hostility using unified processor
        hostile = is_hostile_unified(
            involved_ids=[corp_id],
            system_id=system_id,
            is_asset=True
        )

        if hostile:
            row_tpl = '<tr><td>{}</td><td style="color: red;">{}</td><td>{}</td></tr>'
        else:
            row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td></tr>'

        html_output += format_html(row_tpl, display_name, oname, rname)

    html_output += "</tbody></table>"
    return html_output
