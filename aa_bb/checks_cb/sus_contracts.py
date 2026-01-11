"""
Corporate contract intelligence helpers.

The corporate dashboard reuses these helpers to normalize corp contracts,
highlight hostile counterparties, and cache note text for notifications.
"""

import html
from typing import Dict, Optional, List
from datetime import datetime

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)

from ..app_settings import (
    get_character_id,
    get_eve_entity_type,
    get_entity_info,
    get_safe_entities,
    aablacklist_active,
    resolve_location_name,
    is_location_hostile,
    get_system_owner,
    get_hostile_state,
    corptools_active,
    is_hostile_unified
)

if aablacklist_active():
    from aa_bb.checks.add_to_blacklist import check_char_add_to_bl

try:
    if corptools_active():
        from corptools.models import CorporateContract, CorporationAudit
    else:
        CorporateContract = None
        CorporationAudit = None
except ImportError:
    CorporateContract = None
    CorporationAudit = None
from ..models import BigBrotherConfig, ProcessedContract, SusContractNote
from django.utils import timezone



def _find_employment_at(employment: list, date: datetime) -> Optional[dict]:
    """Return the employment record covering the given timestamp."""
    for i, rec in enumerate(employment):
        start = rec.get('start_date')
        end = rec.get('end_date')
        if start and start <= date and (end is None or date < end):  # Match when contract dates fall within the stint.
            return rec
    return None


def _find_alliance_at(history: list, date: datetime) -> Optional[int]:
    """Return the alliance id a corporation belonged to during the date."""
    for i, rec in enumerate(history):
        start = rec.get('start_date')
        if i + 1 < len(history):  # Use the next record to establish the end boundary.
            next_start = history[i+1]['start_date']
        else:  # Last record in the list gets an open-ended window.
            next_start = None
        if start and start <= date and (next_start is None or date < next_start):  # Same overlap logic for alliance history.
            return rec.get('alliance_id')
    return None


def gather_user_contracts(corp_id: int):
    """
    Fetch every CorporateContract row for the given corporation id.
    """
    if not corptools_active() or CorporateContract is None:
        return []
    try:
        corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
        corp_audit = CorporationAudit.objects.get(corporation=corp_info)
    except (EveCorporationInfo.DoesNotExist, CorporationAudit.DoesNotExist):
        return CorporateContract.objects.none()

    qs = CorporateContract.objects.filter(corporation=corp_audit)
    return qs

def get_user_contracts(qs) -> Dict[int, Dict]:
    """
    Fetch contracts for a user, extracting issuer and assignee details
    with corp/alliance names at the contract issue date, combined.
    Uses c.for_corporation to identify corporate assignees.
    """
    result: Dict[int, Dict] = {}

    _info_cache: Dict[tuple[int, int], dict] = {}

    def _cached_info(eid: int, when: datetime) -> dict:
        key = (int(eid or 0), int(when.date().toordinal()))
        if key in _info_cache:
            return _info_cache[key]
        info = get_entity_info(eid, when)
        if not info:
            info = {'name': 'Unknown', 'corp_id': None, 'corp_name': 'Unknown', 'alli_id': None, 'alli_name': 'Unknown'}
        _info_cache[key] = info
        return info

    for c in qs:
        cid = c.contract_id
        issue = c.date_issued
        timeee = issue or timezone.now()

        # -- issuer --
        issuer_id = get_character_id(c.issuer_name)
        iinfo = _cached_info(issuer_id, timeee)

        # -- assignee --
        if c.assignee_id != 0:  # Corporate contracts specify assignee_id directly.
            assignee_id = c.assignee_id
        else:
            assignee_id = c.acceptor_id

        ainfo = _cached_info(assignee_id, timeee)

        result[cid] = {
            'contract_id':              cid,
            'issued_date':              issue,
            'end_date':                 c.date_completed or c.date_expired,
            'contract_type':            c.contract_type,
            'issuer_name':              iinfo["name"],
            'issuer_id':                issuer_id,
            'issuer_corporation':       iinfo["corp_name"],
            'issuer_corporation_id':    iinfo["corp_id"],
            'issuer_alliance':          iinfo["alli_name"],
            'issuer_alliance_id':       iinfo["alli_id"],
            'assignee_name':            ainfo["name"],
            'assignee_id':              assignee_id,
            'assignee_corporation':     ainfo["corp_name"],
            'assignee_corporation_id':  ainfo["corp_id"],
            'assignee_alliance':        ainfo["alli_name"],
            'assignee_alliance_id':     ainfo["alli_id"],
            'status':                   c.status,
            'start_location_id':        getattr(c, "start_location_id", None),
            'start_location':           resolve_location_name(getattr(c, "start_location_id", None)) or "Unknown Location",
            'end_location_id':          getattr(c, "end_location_id", None),
            'end_location':             resolve_location_name(getattr(c, "end_location_id", None)) or "Unknown Location",
        }
    return result

def get_cell_style_for_contract_row(column: str, row: dict) -> str:
    """Return inline CSS so tables/exports highlight blacklist/hostile hits."""
    when = row.get("issued_date")
    if column == 'issuer_name':  # Color issuer names if the character is suspect.
        cid = row.get("issuer_id")
        if get_hostile_state(cid, 'character', when=when):
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_name':  # Color assignee names when suspect.
        cid = row.get("assignee_id")
        if get_hostile_state(cid, 'character', when=when):
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_corporation':  # Apply styles for hostile issuer corps.
        aid = row.get("issuer_corporation_id")
        if get_hostile_state(aid, 'corporation', when=when):
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_alliance':  # Apply styles for hostile issuer alliances.
        coid = row.get("issuer_alliance_id")
        if get_hostile_state(coid, 'alliance', when=when):
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_corporation':  # Apply styles for hostile assignee corps.
        aid = row.get("assignee_corporation_id")
        if get_hostile_state(aid, 'corporation', when=when):
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_alliance':  # Apply styles for hostile assignee alliances.
        coid = row.get("assignee_alliance_id")
        if get_hostile_state(coid, 'alliance', when=when):
            return 'color: red;'
        else:
            return ''

    return ''

def is_contract_row_hostile(row: dict, safe_entities: set = None) -> bool:
    """
    Checks if a contract is considered hostile using the unified processor.
    Checks both start and end locations.
    """
    issuer_id = row.get("issuer_id")
    assignee_id = row.get("assignee_id")
    when = row.get("issued_date")
    start_loc = row.get("start_location_id")
    end_loc = row.get("end_location_id")

    # Check start location
    if is_hostile_unified(involved_ids=[issuer_id, assignee_id], location_id=start_loc, when=when, safe_entities=safe_entities):
        return True

    # Check end location
    if is_hostile_unified(involved_ids=[issuer_id, assignee_id], location_id=end_loc, when=when, safe_entities=safe_entities):
        return True

    return False


def get_corp_hostile_contracts(corp_id: int) -> Dict[int, str]:
    """
    Return {contract_id -> note} entries for newly detected hostile corp contracts.

    Notes are persisted so subsequent runs simply reuse previously generated
    text while remaining idempotent.
    """
    cfg = BigBrotherConfig.get_solo()
    from ..app_settings import get_safe_entities
    safe_entities = get_safe_entities()

    # 1) Gather all raw contracts
    all_qs = gather_user_contracts(corp_id)
    all_ids = list(all_qs.values_list('contract_id', flat=True))

    # 2) Which are already processed?
    seen_ids = set(ProcessedContract.objects.filter(contract_id__in=all_ids)
                                      .values_list('contract_id', flat=True))

    notes: Dict[int, str] = {}
    new_ids = []
    for cid in all_ids:
        if cid not in seen_ids:  # Track contract ids that still need note generation.
            new_ids.append(cid)
    del all_ids
    del seen_ids
    processed = 0
    if new_ids:  # Only hydrate contracts that haven't been processed.
        processed += 1
        # 3) Hydrate only new contracts
        new_qs = all_qs.filter(contract_id__in=new_ids)
        del all_qs
        new_rows = get_user_contracts(new_qs)

        for cid, c in new_rows.items():
            # only create ProcessedContract if it doesn't already exist
            pc, created = ProcessedContract.objects.get_or_create(contract_id=cid)
            # Skip entries already handled by another worker.
            if not created:
                continue

            if not is_contract_row_hostile(c, safe_entities=safe_entities):  # Skip non-hostile contracts to limit note noise.
                continue

            flags: List[str] = []
            # issuer
            if get_hostile_state(c['issuer_id'], 'character'):
                flags.append(f"Issuer **{c['issuer_name']}** is hostile/blacklisted")
            # fall back just in case
            if get_hostile_state(c['issuer_corporation_id'], 'corporation'):
                flags.append(f"Issuer corp **{c['issuer_corporation']}** is hostile")
            # assignee
            if get_hostile_state(c['assignee_id'], 'character'):
                flags.append(f"Assignee **{c['assignee_name']}** is hostile/blacklisted")
            # fall back just in case
            if get_hostile_state(c['assignee_corporation_id'], 'corporation'):
                flags.append(f"Assignee corp **{c['assignee_corporation']}** is hostile")

            if is_location_hostile(c.get("start_location_id")):
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

            if is_location_hostile(c.get("end_location_id")):
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

            flags_text = "\n    - ".join(flags)

            note_text = (
                f"- **{c['contract_type']}** ({c['issued_date']} → {c['end_date']})"
                f"\n  - **From:** {c['issuer_name']} ({c['issuer_corporation']} | {c['issuer_alliance']})"
                f"\n  - **To:** {c['assignee_name']} ({c['assignee_corporation']} | {c['assignee_alliance']})"
                f"\n  - **Location:** {c['start_location']} → {c['end_location']}"
                f"\n  - **Flags:**"
                f"\n    - {flags_text}"
            )
            SusContractNote.objects.update_or_create(
                contract=pc,
                defaults={'user_id': corp_id, 'note': note_text}
            )
            notes[cid] = note_text

    # 4) Pull in old notes
    for scn in SusContractNote.objects.filter(user_id=corp_id):
        notes[scn.contract.contract_id] = scn.note

    return notes
