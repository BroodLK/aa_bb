"""
Contract intelligence helpers.

The functions in this module normalize Contract ORM rows, highlight hostile
counterparties, and persist short notes for reuse in notifications.
"""

from allianceauth.services.hooks import get_extension_logger


from typing import Dict, Optional, List
from datetime import datetime

from ..app_settings import (
    get_user_characters,
    get_entity_info,
    get_safe_entities,
    aablacklist_active,
    resolve_location_name,
    resolve_location_system_id,
    is_location_hostile,
    get_system_owner,
    get_hostile_state,
    is_highsec,
    is_lowsec,
    corptools_active,
    is_hostile_unified,
)
from django.utils import timezone

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

from django.db.models import Q
from ..models import BigBrotherConfig, ProcessedContract, SusContractNote, EntityInfoCache

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import Contract
    else:
        Contract = None
except ImportError:
    Contract = None


def _find_employment_at(employment: list, date: datetime) -> Optional[dict]:
    for rec in employment:
        start = rec.get("start_date")
        end = rec.get("end_date")
        if start and start <= date and (end is None or date < end):
            return rec
    return None


def _find_alliance_at(history: list, date: datetime) -> Optional[int]:
    for i, rec in enumerate(history):
        start = rec.get("start_date")
        next_start = history[i + 1]["start_date"] if i + 1 < len(history) else None
        if start and start <= date and (next_start is None or date < next_start):
            return rec.get("alliance_id")
    return None


def gather_user_contracts(user_id: int):
    if not corptools_active() or Contract is None:
        return []
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    return Contract.objects.filter(
        character__character__character_id__in=user_ids
    ).select_related("character__character")


def get_user_contracts(qs) -> Dict[int, Dict]:
    # Use a list to avoid re-evaluating the queryset
    entries = list(qs)
    if not entries:
        return {}

    result: Dict[int, Dict] = {}

    # Pre-collect all entity IDs and their normalized timestamps for bulk fetching info
    # EntityInfoCache normalizes to the hour.
    lookups = set()
    for c in entries:
        issue = c.date_issued
        timeee = getattr(c, "timestamp", None) or issue or timezone.now()
        dt_hour = timeee.replace(minute=0, second=0, microsecond=0)

        # issuer_name.eve_id is issuer character id
        issuer_id = c.issuer_name.eve_id
        lookups.add((issuer_id, dt_hour))

        assignee_id = c.assignee_id if c.assignee_id != 0 else c.acceptor_id
        if assignee_id:
            lookups.add((assignee_id, dt_hour))

    # Bulk fetch EntityInfoCache entries
    # To avoid a massive Q-object chain for very large batches, we fetch by entity_id
    # and filter by hour as well to limit the result set.
    eids = {l[0] for l in lookups}
    hours = {l[1] for l in lookups}
    cache_entries = EntityInfoCache.objects.filter(entity_id__in=eids, as_of__in=hours)

    # Map them by (entity_id, as_of)
    info_map = {
        (ce.entity_id, ce.as_of): ce.data
        for ce in cache_entries
    }

    def _get_info(eid: int, when: datetime) -> dict:
        if not eid:
            return get_entity_info(None, when)

        dt_hour = when.replace(minute=0, second=0, microsecond=0)
        cached = info_map.get((eid, dt_hour))
        if cached:
            return cached

        # Fallback to single fetch
        info = get_entity_info(eid, when)
        info_map[(eid, dt_hour)] = info
        return info

    for c in entries:
        cid = c.contract_id
        issue = c.date_issued
        timeee = getattr(c, "timestamp", None) or issue or timezone.now()

        issuer_id = c.issuer_name.eve_id
        iinfo = _get_info(issuer_id, timeee)

        assignee_id = c.assignee_id if c.assignee_id != 0 else c.acceptor_id
        ainfo = _get_info(assignee_id, timeee)

        result[cid] = {
            "contract_id": cid,
            "issued_date": issue,
            "end_date": c.date_completed or c.date_expired,
            "contract_type": c.contract_type,
            "issuer_name": iinfo["name"],
            "issuer_id": issuer_id,
            "issuer_corporation": iinfo["corp_name"],
            "issuer_corporation_id": iinfo["corp_id"],
            "issuer_alliance": iinfo["alli_name"],
            "issuer_alliance_id": iinfo["alli_id"],
            "assignee_name": ainfo["name"],
            "assignee_id": assignee_id,
            "assignee_corporation": ainfo["corp_name"],
            "assignee_corporation_id": ainfo["corp_id"],
            "assignee_alliance": ainfo["alli_name"],
            "assignee_alliance_id": ainfo["alli_id"],
            "status": c.status,
            "start_location_id": getattr(c, "start_location_id", None),
            "start_location": resolve_location_name(getattr(c, "start_location_id", None)) or "Unknown Location",
            "end_location_id": getattr(c, "end_location_id", None),
            "end_location": resolve_location_name(getattr(c, "end_location_id", None)) or "Unknown Location",
            "info_cache": {
                issuer_id: iinfo,
                assignee_id: ainfo
            }
        }

    logger.debug("Hydrated %d contract rows", len(result))
    return result


def get_cell_style_for_contract_row(column: str, row: dict) -> str:
    when = row.get("issued_date")
    info_cache = row.get("info_cache")
    if column == "issuer_name":
        iid = row.get("issuer_id")
        if get_hostile_state(iid, 'character', when=when, entity_info_cache=info_cache):
            return "color: red;"
    if column == "assignee_name":
        aid = row.get("assignee_id")
        if get_hostile_state(aid, 'character', when=when, entity_info_cache=info_cache):
            return "color: red;"

    if column == "issuer_corporation":
        cid = row.get("issuer_corporation_id")
        if get_hostile_state(cid, 'corporation', when=when, entity_info_cache=info_cache):
            return "color: red;"
        return ""

    if column == "issuer_alliance":
        aid = row.get("issuer_alliance_id")
        if get_hostile_state(aid, 'alliance', when=when, entity_info_cache=info_cache):
            return "color: red;"
        return ""

    if column == "assignee_corporation":
        cid = row.get("assignee_corporation_id")
        if get_hostile_state(cid, 'corporation', when=when, entity_info_cache=info_cache):
            return "color: red;"
        return ""

    if column == "assignee_alliance":
        aid = row.get("assignee_alliance_id")
        if get_hostile_state(aid, 'alliance', when=when, entity_info_cache=info_cache):
            return "color: red;"
        return ""

    return ""


def is_contract_row_hostile(row: dict, safe_entities: set = None, cfg: BigBrotherConfig = None) -> bool:
    """
    Checks if a contract is considered hostile using the unified processor.
    Checks both start and end locations.
    """
    issuer_id = row.get("issuer_id")
    assignee_id = row.get("assignee_id")
    when = row.get("issued_date")
    start_loc = row.get("start_location_id")
    end_loc = row.get("end_location_id")
    info_cache = row.get("info_cache")

    # Unified check handles Rule 1 (Safe entities), location rules, and entity rules.
    # Check start location
    if is_hostile_unified(involved_ids=[issuer_id, assignee_id], location_id=start_loc, when=when, safe_entities=safe_entities, entity_info_cache=info_cache, cfg=cfg):
        return True

    # Check end location
    if is_hostile_unified(involved_ids=[issuer_id, assignee_id], location_id=end_loc, when=when, safe_entities=safe_entities, entity_info_cache=info_cache, cfg=cfg):
        return True

    return False


def get_user_hostile_contracts(user_id: int, cfg: BigBrotherConfig = None, safe_entities: set = None, entity_info_cache: dict = None) -> Dict[int, str]:
    if cfg is None:
        cfg = BigBrotherConfig.get_solo()
    if safe_entities is None:
        from ..app_settings import get_safe_entities
        safe_entities = get_safe_entities()

    all_qs = gather_user_contracts(user_id)
    all_ids = list(all_qs.values_list("contract_id", flat=True))

    seen_ids = set(
        ProcessedContract.objects.filter(contract_id__in=all_ids).values_list("contract_id", flat=True)
    )

    notes: Dict[int, str] = {}
    new_ids = [cid for cid in all_ids if cid not in seen_ids]

    # Build hauling corp exclusion set
    hauling_corps = set()
    if cfg.exclude_hauling_corps_from_courier:
        # Built-in major hauling corps
        hauling_corps = {98681117, 98079862, 98421812, 384667640, 1495741119}
        # Add custom corps from config
        if cfg.custom_hauling_corps:
            custom_corps = [int(c.strip()) for c in cfg.custom_hauling_corps.split(",") if c.strip().isdigit()]
            hauling_corps.update(custom_corps)

    if new_ids:
        new_qs = all_qs.filter(contract_id__in=new_ids)
        new_rows = get_user_contracts(new_qs)

        # Mark all new contracts as processed to prevent re-processing safe or excluded ones
        ProcessedContract.objects.bulk_create(
            [ProcessedContract(contract_id=cid) for cid in new_ids],
            ignore_conflicts=True,
        )

        # Filter hostile contracts, excluding courier contracts with hauling corps if enabled
        hostile_rows: dict[int, dict] = {}
        for cid, c in new_rows.items():
            # Check if this is a courier contract with hauling corp (if exclusion enabled)
            if (cfg.exclude_hauling_corps_from_courier and
                c.get("contract_type") == "courier" and
                (c.get("issuer_corporation_id") in hauling_corps or
                 c.get("assignee_corporation_id") in hauling_corps)):
                # Skip this courier contract
                continue

            # Check if contract is hostile
            if is_contract_row_hostile(c, safe_entities=safe_entities, cfg=cfg):
                hostile_rows[cid] = c
        if hostile_rows:
            pcs = {
                pc.contract_id: pc
                for pc in ProcessedContract.objects.filter(contract_id__in=hostile_rows.keys())
            }

            for cid, c in hostile_rows.items():
                pc = pcs.get(cid)
                if not pc:
                    continue

                flags: List[str] = []
                # issuer
                if get_hostile_state(c["issuer_id"], "character", safe_entities=safe_entities, entity_info_cache=entity_info_cache, cfg=cfg):
                    flags.append(f"Issuer **{c['issuer_name']}** is hostile/blacklisted")

                # assignee
                if get_hostile_state(c["assignee_id"], "character", safe_entities=safe_entities, entity_info_cache=entity_info_cache, cfg=cfg):
                    flags.append(f"Assignee **{c['assignee_name']}** is hostile/blacklisted")

                if is_location_hostile(c.get("start_location_id"), safe_entities=safe_entities, cfg=cfg):
                    loc_id = c.get("start_location_id")
                    owner_info = get_system_owner({"id": loc_id})
                    oname = owner_info.get("owner_name")
                    rname = owner_info.get("region_name")
                    flag = f"Start location **{c['start_location']}** is hostile space"
                    if oname or rname:
                        info_parts = []
                        if oname:
                            info_parts.append(oname)
                        if rname and rname != "Unknown Region":
                            info_parts.append(f"Region: {rname}")
                        flag += f" ({' | '.join(info_parts)})"
                    flags.append(flag)

                if is_location_hostile(c.get("end_location_id"), safe_entities=safe_entities, cfg=cfg):
                    loc_id = c.get("end_location_id")
                    owner_info = get_system_owner({"id": loc_id})
                    oname = owner_info.get("owner_name")
                    rname = owner_info.get("region_name")
                    flag = f"End location **{c['end_location']}** is hostile space"
                    if oname or rname:
                        info_parts = []
                        if oname:
                            info_parts.append(oname)
                        if rname and rname != "Unknown Region":
                            info_parts.append(f"Region: {rname}")
                        flag += f" ({' | '.join(info_parts)})"
                    flags.append(flag)

                flags_text = "\n    - ".join(flags) if flags else "(no flags)"

                if c['contract_type'] == "item_exchange" or c['start_location'] == c['end_location']:
                    loc_text = c['start_location']
                else:
                    loc_text = f"{c['start_location']} → {c['end_location']}"

                note_text = (
                    f"- **{c['contract_type']}** ({c['issued_date']} → {c['end_date']})"
                    f"\n  - **From:** {c['issuer_name']} ({c['issuer_corporation']} | {c['issuer_alliance']})"
                    f"\n  - **To:** {c['assignee_name']} ({c['assignee_corporation']} | {c['assignee_alliance']})"
                    f"\n  - **Location:** {loc_text}"
                    f"\n  - **Flags:**"
                    f"\n    - {flags_text}"
                )

                SusContractNote.objects.update_or_create(
                    contract=pc,
                    defaults={"user_id": user_id, "note": note_text},
                )
                notes[cid] = note_text

    for scn in SusContractNote.objects.filter(user_id=user_id):
        notes[scn.contract.contract_id] = scn.note

    return notes
