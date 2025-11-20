"""
Corporate contract intelligence helpers.

The corporate dashboard reuses these helpers to normalize corp contracts,
highlight hostile counterparties, and cache note text for notifications.
"""

import html
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from typing import Dict, Optional, List
from datetime import datetime

from allianceauth.eveonline.models import EveCorporationInfo

from ..app_settings import (
    get_character_id,
    get_eve_entity_type,
    get_entity_info,

)
from aa_bb.checks.corp_blacklist import check_char_corp_bl
try:
    from corptools.models import CorporateContract, CorporationAudit
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")
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
    corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
    corp_audit = CorporationAudit.objects.get(corporation=corp_info)

    qs = CorporateContract.objects.filter(corporation=corp_audit)
    return qs

def get_user_contracts(qs) -> Dict[int, Dict]:
    """
    Fetch contracts for a user, extracting issuer and assignee details
    with corp/alliance names at the contract issue date, combined.
    Uses c.for_corporation to identify corporate assignees.
    """
    logger.info(f"Number of contracts: {len(qs)}")
    number = 0
    result: Dict[int, Dict] = {}
    for c in qs:
        cid = c.contract_id
        issue = c.date_issued
        number += 1
        logger.info(f"corp contract number: {number}")

        # -- issuer --
        issuer_id = get_character_id(c.issuer_name)
        issuer_type = get_eve_entity_type(issuer_id)
        timeee = getattr(c, "timestamp", timezone.now())
        iinfo = get_entity_info(issuer_id, timeee)

        # -- assignee --
        if c.assignee_id != 0:  # Corporate contracts specify assignee_id directly.
            assignee_id = c.assignee_id
        else:
            assignee_id = c.acceptor_id

        assignee_type = get_eve_entity_type(assignee_id)
        ainfo = get_entity_info(assignee_id, timeee)


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
        }
    logger.info(f"Number of contracts returned: {len(result)}")
    return result

def get_cell_style_for_contract_row(column: str, row: dict) -> str:
    """Return inline CSS so tables/exports highlight blacklist/hostile hits."""
    if column == 'issuer_name':  # Color issuer names if the character is suspect.
        cid = row.get("issuer_id")
        if check_char_corp_bl(cid):  # Issuer is on the blacklist.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_name':  # Color assignee names when suspect.
        cid = row.get("assignee_id")
        if check_char_corp_bl(cid):  # Assignee is on the blacklist.
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_corporation':  # Apply styles for hostile issuer corps.
        aid = row.get("issuer_corporation_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_corporations:  # Hostile corp id.
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_alliance':  # Apply styles for hostile issuer alliances.
        coid = row.get("issuer_alliance_id")
        if coid and str(coid) in BigBrotherConfig.get_solo().hostile_alliances:  # Hostile alliance id.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_corporation':  # Apply styles for hostile assignee corps.
        aid = row.get("assignee_corporation_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_corporations:  # Hostile assignee corp.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_alliance':  # Apply styles for hostile assignee alliances.
        coid = row.get("assignee_alliance_id")
        if coid and str(coid) in BigBrotherConfig.get_solo().hostile_alliances:  # Hostile assignee alliance.
            return 'color: red;'
        else:
            return ''

    return ''

def is_contract_row_hostile(row: dict) -> bool:
    """Returns True if the row matches hostile corp/char/alliance criteria."""
    if check_char_corp_bl(row.get("issuer_id")):  # Issuer character/alt is blacklisted.
        return True
    if check_char_corp_bl(row.get("assignee_id")):  # Assignee/acceptor is blacklisted.
        return True

    solo = BigBrotherConfig.get_solo()

    if row.get("issuer_corporation_id") and str(row["issuer_corporation_id"]) in solo.hostile_corporations:  # Issuer corp hostile.
        return True
    if row.get("issuer_alliance_id") and str(row["issuer_alliance_id"]) in solo.hostile_alliances:  # Issuer alliance hostile.
        return True
    if row.get("assignee_corporation_id") and str(row["assignee_corporation_id"]) in solo.hostile_corporations:  # Assignee corp hostile.
        return True
    if row.get("assignee_alliance_id") and str(row["assignee_alliance_id"]) in solo.hostile_alliances:  # Assignee alliance hostile.
        return True

    return False




def get_corp_hostile_contracts(corp_id: int) -> Dict[int, str]:
    """
    Return {contract_id -> note} entries for newly detected hostile corp contracts.

    Notes are persisted so subsequent runs simply reuse previously generated
    text while remaining idempotent.
    """
    cfg = BigBrotherConfig.get_solo()
    hostile_corps = cfg.hostile_corporations
    hostile_allis = cfg.hostile_alliances

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

            if not is_contract_row_hostile(c):  # Skip non-hostile contracts to limit note noise.
                continue

            flags: List[str] = []
            # issuer
            if c['issuer_name'] != '-' and check_char_corp_bl(c['issuer_id']):  # Issuer is blacklisted.
                flags.append(f"Issuer **{c['issuer_name']}** is on blacklist")
            if str(c['issuer_corporation_id']) in hostile_corps:  # Issuer corp matches hostile list.
                flags.append(f"Issuer corp **{c['issuer_corporation']}** is hostile")
            if str(c['issuer_alliance_id']) in hostile_allis:  # Issuer alliance matches hostile list.
                flags.append(f"Issuer alliance **{c['issuer_alliance']}** is hostile")
            # assignee
            if c['assignee_name'] != '-' and check_char_corp_bl(c['assignee_id']):  # Assignee is blacklisted.
                flags.append(f"Assignee **{c['assignee_name']}** is on blacklist")
            if str(c['assignee_corporation_id']) in hostile_corps:  # Assignee corp matches hostile list.
                flags.append(f"Assignee corp **{c['assignee_corporation']}** is hostile")
            if str(c['assignee_alliance_id']) in hostile_allis:  # Assignee alliance matches hostile list.
                flags.append(f"Assignee alliance **{c['assignee_alliance']}** is hostile")
            flags_text = "\n    - ".join(flags)

            note_text = (
                f"- **{c['contract_type']}**: "
                f"\n  - issued **{c['issued_date']}**, "
                f"\n  - ended **{c['end_date']}**, "
                f"\n  - from **{c['issuer_name']}**(**{c['issuer_corporation']}**/"
                  f"**{c['issuer_alliance']}**), "
                f"\n  - to **{c['assignee_name']}**(**{c['assignee_corporation']}**/"
                  f"**{c['assignee_alliance']}**); "
                f"\n  - flags:\n    - {flags_text}"
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
