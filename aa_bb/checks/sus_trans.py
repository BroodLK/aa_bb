"""
Wallet transaction hygiene checks. These helpers normalize journal rows,
flag suspicious counterparties, and keep deduplicated notes for alerts.
"""

import html
import logging
from typing import Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from ..app_settings import (
    get_user_characters,
    get_entity_info,
    aablacklist_active,
)

if aablacklist_active():
    from .corp_blacklist import check_char_corp_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

try:
    from corptools.models import CharacterWalletJournalEntry as WalletJournalEntry
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")

from ..models import BigBrotherConfig, ProcessedTransaction, SusTransactionNote

SUS_TYPES = ("player_trading", "corporation_account_withdrawal", "player_donation")


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


def gather_user_transactions(user_id: int):
    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    qs = WalletJournalEntry.objects.filter(second_party_id__in=user_ids)
    qs = qs.exclude(first_party_id__in=user_ids, second_party_id__in=user_ids)
    return qs


def get_user_transactions(qs) -> Dict[int, Dict]:
    result: Dict[int, Dict] = {}

    _info_cache: dict[tuple[int, int], dict] = {}

    def _cached_info(eid: int, when: datetime) -> dict:
        key = (int(eid or 0), int(when.date().toordinal()))
        if key in _info_cache:
            return _info_cache[key]
        info = get_entity_info(eid, when)
        _info_cache[key] = info
        return info

    for entry in qs:
        tx_id = entry.entry_id
        tx_date = entry.date

        first_party_id = entry.first_party_id
        iinfo = _cached_info(first_party_id, tx_date)

        second_party_id = entry.second_party_id
        ainfo = _cached_info(second_party_id, tx_date)

        context_id = entry.context_id
        context_type = entry.context_id_type
        if context_type == "structure_id":
            context = f"Structure ID: {context_id}"
        elif context_type == "character_id":
            context = f"Character: {_cached_info(context_id, tx_date)['name']}"
        elif context_type == "eve_system":
            context = "EVE System"
        elif context_type is None:
            context = "None"
        elif context_type == "market_transaction_id":
            context = f"Market Transaction ID: {context_id}"
        else:
            context = f"{context_type}: {context_id}"

        result[tx_id] = {
            "entry_id": tx_id,
            "date": tx_date,
            "amount": "{:,}".format(entry.amount),
            "balance": "{:,}".format(entry.balance),
            "description": entry.description,
            "reason": entry.reason,
            "first_party_id": first_party_id,
            "first_party_name": iinfo["name"],
            "first_party_corporation_id": iinfo["corp_id"],
            "first_party_corporation": iinfo["corp_name"],
            "first_party_alliance_id": iinfo["alli_id"],
            "first_party_alliance": iinfo["alli_name"],
            "second_party_id": second_party_id,
            "second_party_name": ainfo["name"],
            "second_party_corporation_id": ainfo["corp_id"],
            "second_party_corporation": ainfo["corp_name"],
            "second_party_alliance_id": ainfo["alli_id"],
            "second_party_alliance": ainfo["alli_name"],
            "context": context,
            "type": entry.ref_type,
        }

    return result


def is_transaction_hostile(tx: dict, user_ids: set = None) -> bool:
    if user_ids and tx.get("first_party_id") in user_ids and tx.get("second_party_id") in user_ids:
        return False

    fp_corp = tx.get("first_party_corporation_id")
    sp_corp = tx.get("second_party_corporation_id")
    if fp_corp and sp_corp and fp_corp == sp_corp:
        return False

    fp_alli = tx.get("first_party_alliance_id")
    sp_alli = tx.get("second_party_alliance_id")
    if fp_alli and sp_alli and fp_alli == sp_alli:
        return False

    cfg = BigBrotherConfig.get_solo()

    if aablacklist_active():
        if check_char_corp_bl(tx.get("first_party_id")) or check_char_corp_bl(tx.get("second_party_id")):
            return True

    wlcorp = set((cfg.whitelist_corporations or "").split(","))
    wlali = set((cfg.whitelist_alliances or "").split(","))
    fpcorp_str = str(fp_corp or "")
    spcorp_str = str(sp_corp or "")
    fpali_str = str(fp_alli or "")
    spali_str = str(sp_alli or "")

    fp_whitelisted = fpcorp_str in wlcorp or fpali_str in wlali
    sp_whitelisted = spcorp_str in wlcorp or spali_str in wlali
    if fp_whitelisted and sp_whitelisted:
        return False

    member_corps = {int(s) for s in (cfg.member_corporations or "").split(",") if s.strip().isdigit()}
    member_allis = {int(s) for s in (cfg.member_alliances or "").split(",") if s.strip().isdigit()}
    ignored_corps = {int(s) for s in (cfg.ignored_corporations or "").split(",") if s.strip().isdigit()}

    def _is_member_or_ignored(corp_id, alli_id) -> bool:
        return (
            (corp_id is not None and corp_id in member_corps)
            or (corp_id is not None and corp_id in ignored_corps)
            or (alli_id is not None and alli_id in member_allis)
        )

    if _is_member_or_ignored(fp_corp, fp_alli) and _is_member_or_ignored(sp_corp, sp_alli):
        return False

    for key in SUS_TYPES:
        if key in (tx.get("type") or ""):
            return True

    for key in ("first_party_corporation_id", "second_party_corporation_id"):
        if tx.get(key) and str(tx[key]) in cfg.hostile_corporations:
            return True
    for key in ("first_party_alliance_id", "second_party_alliance_id"):
        if tx.get(key) and str(tx[key]) in cfg.hostile_alliances:
            return True

    return False


def render_transactions(user_id: int) -> str:
    """
    Render HTML table of recent hostile wallet transactions for user
    """
    qs = gather_user_transactions(user_id)
    txs = get_user_transactions(qs)

    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())

    # sort by date desc
    all_list = sorted(txs.values(), key=lambda x: x['date'], reverse=True)
    hostile = [t for t in all_list if is_transaction_hostile(t, user_ids)]
    if not hostile:  # No transactions require attention.
        return '<p>No hostile transactions found.</p>'

    limit = 50
    display = hostile[:limit]
    skipped = max(0, len(hostile) - limit)

    # define headers to show
    first = display[0]
    HIDDEN = {'first_party_id','second_party_id','first_party_corporation_id','second_party_corporation_id',
              'first_party_alliance_id','second_party_alliance_id','entry_id'}
    headers = [k for k in first.keys() if k not in HIDDEN]

    parts = ['<table class="table table-striped table-hover stats">','<thead>','<tr>']
    for h in headers:
        parts.append(f'<th>{html.escape(h.replace("_"," ").title())}</th>')
    parts.extend(['</tr>','</thead>','<tbody>'])

    for t in display:  # Render each hostile transaction row with contextual styling.
        parts.append('<tr>')
        for col in headers:
            val = html.escape(str(t.get(col)))
            style = ''
            # reuse contract style logic by mapping to transaction
            if col == 'type':
                for key in SUS_TYPES:
                    if key in t['type']:  # Highlight suspicious ref types inline.
                        style = 'color: red;'
            if aablacklist_active():
                if col in ('first_party_name', 'second_party_name') and check_char_corp_bl(t.get(col + '_id', -1)):  # Parties on blacklist.
                    style = 'color: red;'
            if col.endswith('corporation') and t.get(col + '_id') and str(t[col + '_id']) in BigBrotherConfig.get_solo().hostile_corporations:  # Hostile corps.
                style = 'color: red;'
            if col.endswith('alliance') and t.get(col + '_id') and str(t[col + '_id']) in BigBrotherConfig.get_solo().hostile_alliances:  # Hostile alliances.
                style = 'color: red;'
            def make_td(val, style=""):
                """Render a TD with optional inline style for hostile cues."""
                style_attr = f' style="{style}"' if style else ""
                return f"<td{style_attr}>{val}</td>"
            parts.append(make_td(val, style))
        parts.append('</tr>')

    parts.extend(['</tbody>','</table>'])
    if skipped:  # Let the reviewer know older hostile rows are omitted.
        parts.append(f'<p>Showing {limit} of {len(hostile)} hostile transactions; skipped {skipped} older ones.</p>')
    return '\n'.join(parts)


def get_user_hostile_transactions(user_id: int) -> Dict[int, str]:
    qs_all = gather_user_transactions(user_id)
    all_ids = list(qs_all.values_list("entry_id", flat=True))

    seen = set(
        ProcessedTransaction.objects.filter(entry_id__in=all_ids).values_list("entry_id", flat=True)
    )

    notes: Dict[int, str] = {}
    new = [eid for eid in all_ids if eid not in seen]

    if new:
        new_qs = qs_all.filter(entry_id__in=new)
        rows = get_user_transactions(new_qs)

        user_chars = get_user_characters(user_id)
        user_ids = set(user_chars.keys())

        hostile_rows: dict[int, dict] = {eid: tx for eid, tx in rows.items() if is_transaction_hostile(tx, user_ids)}
        if hostile_rows:
            ProcessedTransaction.objects.bulk_create(
                [ProcessedTransaction(entry_id=eid) for eid in hostile_rows.keys()],
                ignore_conflicts=True,
            )
            pts = {
                pt.entry_id: pt
                for pt in ProcessedTransaction.objects.filter(entry_id__in=hostile_rows.keys())
            }

            for eid, tx in hostile_rows.items():
                pt = pts.get(eid)
                if not pt:
                    continue

                flags = []
                ttype = tx.get("type") or ""
                for key in SUS_TYPES:
                    if key in ttype:
                        flags.append(f"Transaction type is **{ttype}**")

                if aablacklist_active():
                    if tx.get("first_party_id") and check_char_corp_bl(tx["first_party_id"]):
                        flags.append(f"first_party **{tx['first_party_name']}** is on blacklist")
                    if tx.get("second_party_id") and check_char_corp_bl(tx["second_party_id"]):
                        flags.append(f"second_party **{tx['second_party_name']}** is on blacklist")

                if str(tx.get("first_party_corporation_id")) in BigBrotherConfig.get_solo().hostile_corporations:
                    flags.append(f"first_party corp **{tx['first_party_corporation']}** is hostile")
                if str(tx.get("first_party_alliance_id")) in BigBrotherConfig.get_solo().hostile_alliances:
                    flags.append(f"first_party alliance **{tx['first_party_alliance']}** is hostile")
                if str(tx.get("second_party_corporation_id")) in BigBrotherConfig.get_solo().hostile_corporations:
                    flags.append(f"second_party corp **{tx['second_party_corporation']}** is hostile")
                if str(tx.get("second_party_alliance_id")) in BigBrotherConfig.get_solo().hostile_alliances:
                    flags.append(f"second_party alliance **{tx['second_party_alliance']}** is hostile")

                flags_lines = [f"    - {flag}" for flag in flags] if flags else ["    - (no extra flags)"]

                note_lines = [
                    f"- **{tx['date']}** · **{tx['amount']} ISK**",
                    (
                        f"  {tx['first_party_name']} "
                        f"({tx['first_party_corporation']} | {tx['first_party_alliance']})"
                        f" **→** "
                        f"{tx['second_party_name']} "
                        f"({tx['second_party_corporation']} | {tx['second_party_alliance']})"
                    ),
                ]

                if tx.get("reason"):
                    note_lines.append(f"  Reason: **{tx['reason']}**")

                note_lines.append("  Flags:")
                note_lines.extend(flags_lines)

                note = "\n".join(note_lines)

                SusTransactionNote.objects.update_or_create(
                    transaction=pt,
                    defaults={"user_id": user_id, "note": note},
                )
                notes[eid] = note

    for note_obj in SusTransactionNote.objects.filter(user_id=user_id):
        notes[note_obj.transaction.entry_id] = note_obj.note

    return notes
