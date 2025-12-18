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
    get_entity_info,
    aablacklist_active,
)
from django.utils import timezone

if aablacklist_active():
    from .corp_blacklist import check_char_corp_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

from ..models import BigBrotherConfig, ProcessedContract, SusContractNote

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

try:
    from corptools.models import Contract
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")


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
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    return Contract.objects.filter(
        character__character__character_id__in=user_ids
    ).select_related("character__character")


def get_user_contracts(qs) -> Dict[int, Dict]:
    try:
        logger.debug("Hydrating %d contracts", qs.count())
    except Exception:
        pass

    result: Dict[int, Dict] = {}
    _info_cache: dict[tuple[int, int], dict] = {}

    def _cached_info(eid: int, when: datetime) -> dict:
        key = (int(eid or 0), int(when.date().toordinal()))
        if key in _info_cache:
            return _info_cache[key]
        info = get_entity_info(eid, when)
        _info_cache[key] = info
        return info

    for c in qs:
        cid = c.contract_id
        issue = c.date_issued
        timeee = getattr(c, "timestamp", None) or issue or timezone.now()

        issuer_id = c.issuer_name.eve_id
        iinfo = _cached_info(issuer_id, timeee)

        assignee_id = c.assignee_id if c.assignee_id != 0 else c.acceptor_id
        ainfo = _cached_info(assignee_id, timeee)

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
        }

    logger.debug("Hydrated %d contract rows", len(result))
    return result


def get_cell_style_for_contract_row(column: str, row: dict) -> str:
    if aablacklist_active():
        if column == "issuer_name" and check_char_corp_bl(row.get("issuer_id")):
            return "color: red;"
        if column == "assignee_name" and check_char_corp_bl(row.get("assignee_id")):
            return "color: red;"

    if column == "issuer_corporation":
        cid = row.get("issuer_corporation_id")
        if cid and str(cid) in BigBrotherConfig.get_solo().hostile_corporations:
            return "color: red;"
        return ""

    if column == "issuer_alliance":
        aid = row.get("issuer_alliance_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_alliances:
            return "color: red;"
        return ""

    if column == "assignee_corporation":
        cid = row.get("assignee_corporation_id")
        if cid and str(cid) in BigBrotherConfig.get_solo().hostile_corporations:
            return "color: red;"
        return ""

    if column == "assignee_alliance":
        aid = row.get("assignee_alliance_id")
        if aid and str(aid) in BigBrotherConfig.get_solo().hostile_alliances:
            return "color: red;"
        return ""

    return ""


def is_contract_row_hostile(row: dict) -> bool:
    if aablacklist_active():
        if check_char_corp_bl(row.get("issuer_id")):
            return True
        if check_char_corp_bl(row.get("assignee_id")):
            return True

    solo = BigBrotherConfig.get_solo()
    if row.get("issuer_corporation_id") and str(row["issuer_corporation_id"]) in solo.hostile_corporations:
        return True
    if row.get("issuer_alliance_id") and str(row["issuer_alliance_id"]) in solo.hostile_alliances:
        return True
    if row.get("assignee_corporation_id") and str(row["assignee_corporation_id"]) in solo.hostile_corporations:
        return True
    if row.get("assignee_alliance_id") and str(row["assignee_alliance_id"]) in solo.hostile_alliances:
        return True
    return False


def get_user_hostile_contracts(user_id: int) -> Dict[int, str]:
    cfg = BigBrotherConfig.get_solo()
    hostile_corps = cfg.hostile_corporations
    hostile_allis = cfg.hostile_alliances

    all_qs = gather_user_contracts(user_id)
    all_ids = list(all_qs.values_list("contract_id", flat=True))

    seen_ids = set(
        ProcessedContract.objects.filter(contract_id__in=all_ids).values_list("contract_id", flat=True)
    )

    notes: Dict[int, str] = {}
    new_ids = [cid for cid in all_ids if cid not in seen_ids]

    if new_ids:
        new_qs = all_qs.filter(contract_id__in=new_ids)
        new_rows = get_user_contracts(new_qs)

        hostile_rows: dict[int, dict] = {cid: c for cid, c in new_rows.items() if is_contract_row_hostile(c)}
        if not hostile_rows:
            return {}

        ProcessedContract.objects.bulk_create(
            [ProcessedContract(contract_id=cid) for cid in hostile_rows.keys()],
            ignore_conflicts=True,
        )

        pcs = {
            pc.contract_id: pc
            for pc in ProcessedContract.objects.filter(contract_id__in=hostile_rows.keys())
        }

        for cid, c in hostile_rows.items():
            pc = pcs.get(cid)
            if not pc:
                continue

            flags: List[str] = []
            if aablacklist_active():
                if c["issuer_name"] != "-" and check_char_corp_bl(c["issuer_id"]):
                    flags.append(f"Issuer **{c['issuer_name']}** is on blacklist")
            if str(c["issuer_corporation_id"]) in hostile_corps:
                flags.append(f"Issuer corp **{c['issuer_corporation']}** is hostile")
            if str(c["issuer_alliance_id"]) in hostile_allis:
                flags.append(f"Issuer alliance **{c['issuer_alliance']}** is hostile")

            if aablacklist_active():
                if c["assignee_name"] != "-" and check_char_corp_bl(c["assignee_id"]):
                    flags.append(f"Assignee **{c['assignee_name']}** is on blacklist")
            if str(c["assignee_corporation_id"]) in hostile_corps:
                flags.append(f"Assignee corp **{c['assignee_corporation']}** is hostile")
            if str(c["assignee_alliance_id"]) in hostile_allis:
                flags.append(f"Assignee alliance **{c['assignee_alliance']}** is hostile")

            flags_text = "\n    - ".join(flags) if flags else "(no flags)"

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
                defaults={"user_id": user_id, "note": note_text},
            )
            notes[cid] = note_text

    for scn in SusContractNote.objects.filter(user_id=user_id):
        notes[scn.contract.contract_id] = scn.note

    return notes
