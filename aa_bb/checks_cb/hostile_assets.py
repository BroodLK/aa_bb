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

def get_asset_locations(corp_id: int) -> Dict[int, dict]:
    """
    Return a dict mapping system IDs to structured data:
    {
        system_id: {
            "name": system_name,
            "locations": {
                loc_id: {
                    "name": loc_name,
                    "has_assets": True
                }
            }
        }
    }
    """
    if not corptools_active() or CorporationAudit is None:
        return {}
    try:
        corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
        corp_audit = CorporationAudit.objects.get(corporation=corp_info)
    except (CorporationAudit.DoesNotExist, EveCorporationInfo.DoesNotExist):
        return {}

    system_map: Dict[int, dict] = {}
    _loc_sys_cache = {}
    _loc_name_cache = {}

    # All corp assets (exclude ones where location_flag is "solar_system")
    assets = CorpAsset.objects.select_related('location_name__system') \
                              .filter(corporation=corp_audit) \
                              .exclude(location_flag="solar_system")

    for asset in assets:
        loc = asset.location_name
        loc_id = getattr(loc, 'id', None)
        system_obj = getattr(loc, 'system', None)

        sid = None
        if system_obj:
            sid = system_obj.pk
            s_name = system_obj.name
        elif loc_id:
            if loc_id in _loc_sys_cache:
                sid = _loc_sys_cache[loc_id]
            else:
                sid = resolve_location_system_id(loc_id)
                _loc_sys_cache[loc_id] = sid

            if sid:
                if sid in _loc_name_cache:
                    s_name = _loc_name_cache[sid]
                else:
                    s_name = resolve_location_name(sid)
                    _loc_name_cache[sid] = s_name

        if sid:
            if sid not in system_map:
                system_map[sid] = {"name": s_name, "locations": {}}

            if loc_id:
                if loc_id not in system_map[sid]["locations"]:
                    l_name = "Unknown Location"
                    if loc_id in _loc_name_cache:
                        l_name = _loc_name_cache[loc_id]
                    else:
                        l_name = resolve_location_name(loc_id)
                        _loc_name_cache[loc_id] = l_name

                    system_map[sid]["locations"][loc_id] = {"name": l_name}

    return system_map

def get_corp_hostile_asset_locations(corp_id: int) -> Dict[str, dict]:
    """
    Return {system name -> structured hostile data} for hostile corp asset locations.
    Uses the unified processor logic.
    """
    systems = get_asset_locations(corp_id)
    if not systems:
        return {}

    hostile_map: Dict[str, dict] = {}
    from ..app_settings import get_safe_entities
    safe_entities = get_safe_entities()

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"Unknown ({system_id})"

        system_has_hostile = False
        records = []

        # Check each location in this system
        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]

            # Check hostility using unified processor
            if is_hostile_unified(
                involved_ids=[corp_id],
                location_id=loc_id,
                system_id=system_id,
                is_asset=True,
                safe_entities=safe_entities
            ):
                system_has_hostile = True
                records.append({
                    "location_name": loc_name
                })

        if system_has_hostile:
            owner_info = get_system_owner({
                "id":   system_id,
                "name": display_name
            })

            oname = owner_info.get("owner_name") or "Unresolvable"
            rname = owner_info.get("region_name") or "Unknown Region"

            hostile_map[display_name] = {
                "owner": oname,
                "region": rname,
                "records": records
            }
            logger.info(f"Hostile corp asset system: {display_name} owned by {oname}")

    return hostile_map


def render_assets(corp_id: int) -> Optional[str]:
    """
    Render an HTML table of systems where the corporation owns assets in space.
    Highlights hostile sovereignty holders in red using the unified processor.
    """
    systems = get_asset_locations(corp_id)
    if not systems:
        return '<table class="table stats"><tbody><tr><td class="text-center">No hostile assets found.</td></tr></tbody></table>'

    html_output = '<table class="table table-striped">'
    html_output += '<thead><tr><th>System</th><th>Location</th><th>Owner</th><th>Region</th></tr></thead><tbody>'
    from ..app_settings import get_safe_entities
    safe_entities = get_safe_entities()

    for system_id, data in systems.items():
        system_name = data.get("name")
        display_name = system_name or f"Unknown ({system_id})"
        owner_info = get_system_owner({
            "id":   system_id,
            "name": display_name
        })

        oname = owner_info.get("owner_name") or "—"
        rname = owner_info.get("region_name") or "—"

        for loc_id, loc_data in data.get("locations", {}).items():
            loc_name = loc_data["name"]

            # Check hostility using unified processor
            hostile = is_hostile_unified(
                involved_ids=[corp_id],
                location_id=loc_id,
                system_id=system_id,
                is_asset=True,
                safe_entities=safe_entities
            )

            if hostile:
                row_tpl = '<tr><td>{}</td><td>{}</td><td style="color: red;" class="text-danger">{}</td><td>{}</td></tr>'
            else:
                row_tpl = '<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>'

            html_output += format_html(row_tpl, display_name, loc_name, oname, rname)

    html_output += "</tbody></table>"
    return html_output
