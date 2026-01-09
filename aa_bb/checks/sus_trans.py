"""
Wallet transaction hygiene checks. These helpers normalize journal rows,
flag suspicious counterparties, and keep deduplicated notes for alerts.
"""

import html
import requests
from typing import Dict, Optional
from datetime import datetime, timedelta
from django.utils import timezone

from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)

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

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

try:
    if corptools_active():
        from corptools.models import (
            CharacterWalletJournalEntry as WalletJournalEntry,
            Structure,
        )
        # Try to import CharacterMarketTransaction, but don't fail if it doesn't exist
        try:
            from corptools.models import CharacterMarketTransaction
        except ImportError:
            CharacterMarketTransaction = None
            logger.warning("CharacterMarketTransaction not available in corptools")

        logger.info(f"Successfully imported WalletJournalEntry: {WalletJournalEntry}")
    else:
        logger.warning("corptools_active() returned False at import time")
        WalletJournalEntry = None
        CharacterMarketTransaction = None
        Structure = None
except ImportError as e:
    logger.error(f"Failed to import corptools models: {e}")
    WalletJournalEntry = None
    CharacterMarketTransaction = None
    Structure = None

from django.apps import apps
EVEUNIVERSE_INSTALLED = apps.is_installed("eveuniverse")
if EVEUNIVERSE_INSTALLED:
    try:
        from eveuniverse.models import EveMarketPrice
    except ImportError:
        EVEUNIVERSE_INSTALLED = False

from ..models import BigBrotherConfig, ProcessedTransaction, SusTransactionNote, EveItemPrice

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
    if not corptools_active() or WalletJournalEntry is None:
        return []

    user_chars = get_user_characters(user_id)
    user_ids = set(user_chars.keys())
    logger.info(f"gather_user_transactions for user {user_id}: user_chars={list(user_ids)}")

    # Check total entries in database
    total_entries = WalletJournalEntry.objects.count()
    logger.info(f"Total CharacterWalletJournalEntry records in database: {total_entries}")

    # Check entries for user's characters
    char_entries = WalletJournalEntry.objects.filter(character__character__character_id__in=user_ids)
    logger.info(f"Entries for user's characters: {char_entries.count()}")

    if char_entries.exists():
        sample = char_entries.first()
        logger.info(f"Sample entry: character_id={sample.character.character.character_id}, first_party={sample.first_party_id}, second_party={sample.second_party_id}, ref_type={sample.ref_type}")

    from django.db.models import Q
    # Filter by character ownership (entries belonging to user's characters)
    qs = WalletJournalEntry.objects.filter(character__character__character_id__in=user_ids)

    # Also filter to transactions involving external parties
    # Keep only transactions where at least one party is NOT the user
    qs = qs.filter(Q(first_party_id__in=user_ids) | Q(second_party_id__in=user_ids))
    logger.info(f"After filtering by first_party_id or second_party_id in user_ids: {qs.count()}")

    qs = qs.exclude(first_party_id__in=user_ids, second_party_id__in=user_ids)
    logger.info(f"After excluding internal transactions: {qs.count()}")

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
        system_id = None
        location_id = None
        type_id = None
        quantity = 1
        if context_type == "structure_id":
            name = resolve_location_name(context_id)
            context = f"Structure: {name}" if name else f"Structure ID: {context_id}"
            location_id = context_id
            system_id = resolve_location_system_id(context_id)
        elif context_type == "character_id":
            context = f"Character: {_cached_info(context_id, tx_date)['name']}"
        elif context_type == "eve_system":
            context = "EVE System"
            system_id = context_id
            location_id = context_id
        elif context_type is None:
            context = "None"
        elif context_type == "market_transaction_id":
            context = f"Market Transaction ID: {context_id}"
            if CharacterMarketTransaction:
                m_tx = CharacterMarketTransaction.objects.filter(transaction_id=context_id).first()
                if m_tx:
                    location_id = m_tx.location_id if hasattr(m_tx, "location_id") else None
                    system_id = m_tx.location.system_id if hasattr(m_tx.location, "system_id") else None
                    type_id = m_tx.type_id
                    quantity = m_tx.quantity
        else:
            context = f"{context_type}: {context_id}"

        result[tx_id] = {
            "entry_id": tx_id,
            "date": tx_date,
            "amount": "{:,}".format(entry.amount),
            "raw_amount": float(entry.amount),
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
            "system_id": system_id,
            "location_id": location_id,
            "type_id": type_id,
            "quantity": quantity,
        }

    return result


def is_transaction_hostile(tx: dict, user_ids: set = None) -> bool:
    """
    Checks if a wallet transaction is considered hostile using the unified processor.
    """
    ttype = tx.get("type") or ""
    is_sus_type = any(st in ttype for st in SUS_TYPES)
    is_market = "market_escrow" in ttype or "market_transaction" in ttype

    if not (is_sus_type or is_market):
        return False

    fpid = tx.get("first_party_id")
    spid = tx.get("second_party_id")

    # Unified check handles Rule 1 (Safe entities), which includes both parties being safe.
    # It also handles hubs, price thresholds, location hostility, etc.
    return is_hostile_unified(
        involved_ids=[fpid, spid],
        location_id=tx.get("location_id"),
        system_id=tx.get("system_id"),
        is_market=is_market,
        market_item_id=tx.get("type_id"),
        market_unit_price=abs(tx.get("raw_amount")) / (tx.get("quantity") or 1) if tx.get("raw_amount") is not None else None,
        when=tx.get("date")
    )


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

    cfg = BigBrotherConfig.get_solo()
    hostile_corps = {s.strip() for s in (cfg.hostile_corporations or "").split(",") if s.strip()}
    hostile_allis = {s.strip() for s in (cfg.hostile_alliances or "").split(",") if s.strip()}
    safe_entities = get_safe_entities()

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
                if cfg.show_market_transactions:
                    if "market_escrow" in t['type'] or "market_transaction" in t['type']:
                        style = 'color: red;'
            if col in ('first_party_name', 'second_party_name'):
                pid = t.get(col + '_id')
                if get_hostile_state(pid, 'character'):
                    style = 'color: red;'
            if col.endswith('corporation'):
                cid = t.get(col + '_id')
                if get_hostile_state(cid, 'corporation'):
                    style = 'color: red;'
            if col.endswith('alliance'):
                aid = t.get(col + '_id')
                if get_hostile_state(aid, 'alliance'):
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

                if BigBrotherConfig.get_solo().show_market_transactions:
                    if "market_escrow" in ttype or "market_transaction" in ttype:
                        flags.append(f"Transaction type is **{ttype}**")

                cfg = BigBrotherConfig.get_solo()

                fpid = tx.get("first_party_id")
                if get_hostile_state(fpid, 'character'):
                    flags.append(f"first_party **{tx['first_party_name']}** is hostile/blacklisted")

                spid = tx.get("second_party_id")
                if get_hostile_state(spid, 'character'):
                    flags.append(f"second_party **{tx['second_party_name']}** is hostile/blacklisted")

                loc_id = tx.get("location_id") or tx.get("system_id")
                if loc_id and is_location_hostile(tx.get("location_id"), tx.get("system_id")):
                    loc_name = resolve_location_name(loc_id) or f"ID {loc_id}"
                    owner_info = get_system_owner({"id": loc_id})
                    oname = owner_info.get("owner_name")
                    rname = owner_info.get("region_name")
                    flag = f"Location **{loc_name}** is hostile space"
                    if oname or rname:
                        info_parts = []
                        if oname:
                            info_parts.append(oname)
                        if rname and rname != "Unknown Region":
                            info_parts.append(f"Region: {rname}")
                        flag += f" ({' | '.join(info_parts)})"
                    flags.append(flag)

                flags_lines = [f"    - {flag}" for flag in flags] if flags else ["    - (no extra flags)"]

                note_lines = [
                    f"- **{tx['date']}** · **{tx['amount']} ISK**",
                    f"  - **Type:** {tx['type']}",
                    (
                        f"  - **From:** {tx['first_party_name']} "
                        f"({tx['first_party_corporation']} | {tx['first_party_alliance']})"
                    ),
                    (
                        f"  - **To:** {tx['second_party_name']} "
                        f"({tx['second_party_corporation']} | {tx['second_party_alliance']})"
                    ),
                ]

                if tx.get("reason") and tx.get("reason") != "None":
                    note_lines.append(f"  - **Reason:** {tx['reason']}")

                if tx.get("context") and tx.get("context") != "None":
                    note_lines.append(f"  - **Context:** {tx['context']}")

                note_lines.append("  - **Flags:**")
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
