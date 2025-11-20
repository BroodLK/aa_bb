"""
Contract intelligence helpers.

The functions in this module normalize Contract ORM rows, highlight hostile
counterparties, and persist short notes for reuse in notifications.
"""

import logging

from typing import Dict, Optional, List
from datetime import datetime

from ..app_settings import (
    get_user_characters,
    get_eve_entity_type,
    get_entity_info,

)
from .corp_blacklist import check_char_corp_bl
from ..models import BigBrotherConfig, ProcessedContract, SusContractNote
from django.utils import timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

try:
    from corptools.models import Contract
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

def _find_employment_at(employment: list, date: datetime) -> Optional[dict]:
    """Utility kept for backwards compatibility; returns corp during date."""
    for i, rec in enumerate(employment):
        start = rec.get('start_date')
        end = rec.get('end_date')
        if start and start <= date and (end is None or date < end):  # Match when date falls inside this employment window.
            return rec
    return None


def _find_alliance_at(history: list, date: datetime) -> Optional[int]:
    """Find the alliance a corp belonged to at the given point in time."""
    for i, rec in enumerate(history):
        start = rec.get('start_date')
        next_start = history[i+1]['start_date'] if i+1 < len(history) else None
        if start and start <= date and (next_start is None or date < next_start):  # Same overlap check for alliance periods.
            return rec.get('alliance_id')
    return None


def gather_user_contracts(user_id: int):
    """Return a queryset with every contract involving the user's characters."""
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    qs = Contract.objects.filter(
        character__character__character_id__in=user_ids
    ).select_related('character__character')
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
        logger.info(f"contract number: {number}")

        # -- issuer --
        issuer_id = c.issuer_name.eve_id
        issuer_type = get_eve_entity_type(issuer_id)
        timeee = getattr(c, "timestamp", timezone.now())
        iinfo = get_entity_info(issuer_id, timeee)

        # -- assignee --
        if c.assignee_id != 0:  # Contracts addressed to a corp/character use assignee_id; otherwise fall back to acceptor.
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
    """
    Inline styling helper shared by renderers and exports to make every
    hostile party show up in red regardless of where the data lands.
    """
    if column == 'issuer_name':
        cid = row.get("issuer_id")
        if check_char_corp_bl(cid):  # Issuer appears on blacklist.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_name':
        cid = row.get("assignee_id")
        if check_char_corp_bl(cid):  # Assignee appears on blacklist.
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_corporation':
        aid = row.get("issuer_corporation_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_corporations:  # Issuer corp is flagged hostile.
            return 'color: red;'
        else:
            return ''

    if column == 'issuer_alliance':
        coid = row.get("issuer_alliance_id")
        if coid and str(coid) in BigBrotherConfig.get_solo().hostile_alliances:  # Issuer alliance flagged hostile.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_corporation':
        aid = row.get("assignee_corporation_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_corporations:  # Assignee corp flagged hostile.
            return 'color: red;'
        else:
            return ''

    if column == 'assignee_alliance':
        coid = row.get("assignee_alliance_id")
        if coid and str(coid) in BigBrotherConfig.get_solo().hostile_alliances:  # Assignee alliance flagged hostile.
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




def get_user_hostile_contracts(user_id: int) -> Dict[int, str]:
    """
    Persist and return a mapping of hostile contract id -> formatted note.

    This keeps background notifications idempotent while still allowing the
    UI to show both new and previously seen alerts.
    """
    cfg = BigBrotherConfig.get_solo()
    hostile_corps = cfg.hostile_corporations
    hostile_allis = cfg.hostile_alliances

    # 1) Gather all raw contracts
    all_qs = gather_user_contracts(user_id)
    all_ids = list(all_qs.values_list('contract_id', flat=True))

    # 2) Which are already processed?
    seen_ids = set(ProcessedContract.objects.filter(contract_id__in=all_ids)
                                      .values_list('contract_id', flat=True))

    notes: Dict[int, str] = {}
    new_ids = [cid for cid in all_ids if cid not in seen_ids]

    if new_ids:  # Only hydrate/stash contracts that have not been seen before.
        # 3) Hydrate only new contracts
        new_qs = all_qs.filter(contract_id__in=new_ids)
        new_rows = get_user_contracts(new_qs)

        for cid, c in new_rows.items():
            # only create ProcessedContract if it doesn't already exist
            pc, created = ProcessedContract.objects.get_or_create(contract_id=cid)
            # Another worker already processed this contract.
            if not created:
                continue

            if not is_contract_row_hostile(c):  # Skip benign contracts entirely.
                continue

            flags: List[str] = []
            # issuer
            if c['issuer_name'] != '-' and check_char_corp_bl(c['issuer_id']):
                flags.append(f"Issuer **{c['issuer_name']}** is on blacklist")
            if str(c['issuer_corporation_id']) in hostile_corps:
                flags.append(f"Issuer corp **{c['issuer_corporation']}** is hostile")
            if str(c['issuer_alliance_id']) in hostile_allis:
                flags.append(f"Issuer alliance **{c['issuer_alliance']}** is hostile")
            # assignee
            if c['assignee_name'] != '-' and check_char_corp_bl(c['assignee_id']):
                flags.append(f"Assignee **{c['assignee_name']}** is on blacklist")
            if str(c['assignee_corporation_id']) in hostile_corps:
                flags.append(f"Assignee corp **{c['assignee_corporation']}** is hostile")
            if str(c['assignee_alliance_id']) in hostile_allis:
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
                defaults={'user_id': user_id, 'note': note_text}
            )
            notes[cid] = note_text

    # 4) Pull in old notes
    for scn in SusContractNote.objects.filter(user_id=user_id):  # Merge past notes so UI shows history.
        notes[scn.contract.contract_id] = scn.note

    return notes
