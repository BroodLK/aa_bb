"""
Corporate wallet journal analysis helpers mirroring the member-level checks.
"""

import html
import requests
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from django.utils import timezone

from allianceauth.eveonline.models import EveCorporationInfo
from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)

from ..app_settings import (
    get_eve_entity_type,
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
    is_hostile_unified
)

if aablacklist_active():
    from aa_bb.checks.add_to_blacklist import check_char_add_to_bl

try:
    if corptools_active():
        from corptools.models import (
            CorporationAudit,
            CorporationWalletJournalEntry,
            Structure,
        )
        # Try to import CorporationMarketTransaction, but don't fail if it doesn't exist
        try:
            from corptools.models import CorporationMarketTransaction
        except ImportError:
            CorporationMarketTransaction = None
    else:
        CorporationAudit = None
        CorporationWalletJournalEntry = None
        CorporationMarketTransaction = None
        Structure = None
except ImportError:
    CorporationAudit = None
    CorporationWalletJournalEntry = None
    CorporationMarketTransaction = None
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
    """Compat helper that returns the corp active at the provided date."""
    for i, rec in enumerate(employment):
        start = rec.get('start_date')
        end = rec.get('end_date')
        if start and start <= date and (end is None or date < end):  # Match when the timestamp falls inside the stint.
            return rec
    return None


def _find_alliance_at(history: list, date: datetime) -> Optional[int]:
    """Compat helper returning the alliance id active during the period."""
    for i, rec in enumerate(history):
        start = rec.get('start_date')
        if i + 1 < len(history):  # Use the next record to bound the range.
            next_start = history[i+1]['start_date']
        else:  # Open ended when last history entry.
            next_start = None
        if start and start <= date and (next_start is None or date < next_start):  # Same overlap logic for alliance history.
            return rec.get('alliance_id')
    return None


def gather_user_transactions(corp_id: int):
    """
    Return a queryset of every wallet journal entry for the corp divisions.

    Parameter mirrors the member helper naming but expects a corporation id.
    """
    if not corptools_active() or CorporationWalletJournalEntry is None:
        return []
    try:
        corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
        corp_audit = CorporationAudit.objects.get(corporation=corp_info)
    except (EveCorporationInfo.DoesNotExist, CorporationAudit.DoesNotExist):
        return CorporationWalletJournalEntry.objects.none()

    qs = CorporationWalletJournalEntry.objects.filter(division__corporation=corp_audit)
    logger.info(f"Gathered {qs.count()} wallet entries for corp {corp_id}")
    return qs


def get_user_transactions(qs) -> Dict[int, Dict]:
    """
    Transform raw WalletJournalEntry queryset into structured dict
    with first_party (first_party) and second_party (second_party) info,
    resolving corp/alliance at transaction time.
    """
    result: Dict[int, Dict] = {}

    local_cache: dict = {}

    def _cached_info(eid: int, when: datetime) -> dict:
        return get_entity_info(eid, when, local_cache=local_cache)

    for entry in qs:
        tx_id = entry.entry_id
        tx_date = entry.date

        # first_party = first_party_id
        first_party_id = entry.first_party_id
        iinfo = _cached_info(first_party_id, tx_date)

        # second_party = second_party_id
        second_party_id = entry.second_party_id
        ainfo = _cached_info(second_party_id, tx_date)

        context = ""
        context_id = entry.context_id
        context_type = entry.context_id_type
        system_id = None
        location_id = None
        type_id = None
        quantity = 1
        if context_type == "structure_id":  # Provide human-readable structure context.
            name = resolve_location_name(context_id)
            context = f"Structure: {name}" if name else f"Structure ID: {context_id}"
            location_id = context_id
            system_id = resolve_location_system_id(context_id)
        elif context_type == "character_id":  # Link to a specific character.
            char_info = _cached_info(context_id, tx_date)
            char_name = char_info['name'] if char_info else 'Unknown'
            context = f"Character: {char_name}"
        elif context_type == "eve_system":  # System-level context from journal entry.
            context = "EVE System"
            system_id = context_id
            location_id = context_id
        elif context_type is None:  # No extra context provided.
            context = "None"
        elif context_type == "market_transaction_id":  # Reference to market transaction.
            context = f"Market Transaction ID: {context_id}"
            if CorporationMarketTransaction:
                m_tx = CorporationMarketTransaction.objects.filter(transaction_id=context_id).first()
                if m_tx:
                    location_id = m_tx.location_id if hasattr(m_tx, "location_id") else None
                    system_id = m_tx.location.system_id if hasattr(m_tx.location, "system_id") else None
                    type_id = m_tx.type_id
                    quantity = m_tx.quantity
        else:  # Fallback for any future context types.
            context = f"{context_type}: {context_id}"

        amount =  "{:,}".format(entry.amount)
        balance =  "{:,}".format(entry.balance)

        result[tx_id] = {
            'entry_id': tx_id,
            'date': tx_date,
            'amount': amount,
            'raw_amount': float(entry.amount),
            'balance': balance,
            'description': entry.description,
            'reason': entry.reason,
            'first_party_id': first_party_id,
            'first_party_name': iinfo['name'],
            'first_party_corporation_id': iinfo['corp_id'],
            'first_party_corporation': iinfo['corp_name'],
            'first_party_alliance_id': iinfo['alli_id'],
            'first_party_alliance': iinfo['alli_name'],
            'second_party_id': second_party_id,
            'second_party_name': ainfo['name'],
            'second_party_corporation_id': ainfo['corp_id'],
            'second_party_corporation': ainfo['corp_name'],
            'second_party_alliance_id': ainfo['alli_id'],
            'second_party_alliance': ainfo['alli_name'],
            'context': context,
            'type': entry.ref_type,
            'system_id': system_id,
            'location_id': location_id,
            'type_id': type_id,
            'quantity': quantity,
        }
    return result


def is_transaction_hostile(tx: dict, safe_entities: set = None, local_cache: dict = None) -> bool:
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

    return is_hostile_unified(
        involved_ids=[fpid, spid],
        location_id=tx.get("location_id"),
        system_id=tx.get("system_id"),
        is_market=is_market,
        market_item_id=tx.get("type_id"),
        market_unit_price=abs(tx.get("raw_amount")) / (tx.get("quantity") or 1) if tx.get("raw_amount") is not None else None,
        when=tx.get("date"),
        safe_entities=safe_entities,
        local_cache=local_cache
    )


def render_transactions(corp_id: int) -> str:
    """
    Render HTML table of recent hostile wallet transactions for the corp.
    """
    try:
        qs = gather_user_transactions(corp_id)
        txs = get_user_transactions(qs)

        from ..app_settings import get_safe_entities
        safe_entities = get_safe_entities()
        local_cache = {}

        # sort by date desc
        all_list = sorted(txs.values(), key=lambda x: x['date'], reverse=True)
        hostile: List[dict] = []
        for tx in all_list:
            if is_transaction_hostile(tx, safe_entities=safe_entities, local_cache=local_cache):  # Keep only transactions that tripped hostility logic.
                hostile.append(tx)
        if not hostile:  # No hostile rows were identified.
            return '<p>No hostile transactions found.</p>'

        limit = 50
        display = hostile[:limit]
        skipped = max(0, len(hostile) - limit)

        # define headers to show
        first = display[0]
        HIDDEN = {'first_party_id','second_party_id','first_party_corporation_id','second_party_corporation_id',
                  'first_party_alliance_id','second_party_alliance_id','entry_id'}
        headers = []
        for column in first.keys():
            if column not in HIDDEN:  # Hide ids/foreign keys that are not user-facing.
                headers.append(column)

        parts = ['<table class="table table-striped table-hover stats">','<thead>','<tr>']
        for h in headers:
            parts.append(f'<th>{html.escape(h.replace("_"," ").title())}</th>')
        parts.extend(['</tr>','</thead>','<tbody>'])

        cfg = BigBrotherConfig.get_solo()
        hostile_corps = {s.strip() for s in (cfg.hostile_corporations or "").split(",") if s.strip()}
        hostile_allis = {s.strip() for s in (cfg.hostile_alliances or "").split(",") if s.strip()}

        for t in display:
            parts.append('<tr>')
            for col in headers:
                val = html.escape(str(t.get(col)))
                style = ''
                # reuse contract style logic by mapping to transaction
                if col == 'type':  # Highlight suspicious ref types inline.
                    for key in SUS_TYPES:
                        if key in t['type']:  # Suspect ref-type.
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
    except Exception as e:
        logger.exception(f"Error rendering transactions for corp {corp_id}")
        return f"<p class='text-danger'>Error rendering transactions: {str(e)}</p>"


def get_corp_hostile_transactions(corp_id: int, safe_entities: set = None, local_cache: dict = None) -> Dict[int, str]:
    """
    Persist and return formatted notes for hostile corporate transactions.
    """
    qs_all = gather_user_transactions(corp_id)
    all_ids = list(qs_all.values_list('entry_id', flat=True))
    seen = set(ProcessedTransaction.objects.filter(entry_id__in=all_ids)
                                              .values_list('entry_id', flat=True))
    notes: Dict[int, str] = {}
    new: List[int] = []
    for eid in all_ids:
        if eid not in seen:  # Only keep transactions that need processing.
            new.append(eid)
    del all_ids
    del seen
    processed = 0
    if new:  # Only hydrate rows when new entry ids exist.
        processed += 1
        new_qs = qs_all.filter(entry_id__in=new)
        del qs_all
        rows = get_user_transactions(new_qs)

        from ..app_settings import get_safe_entities
        if safe_entities is None:
            safe_entities = get_safe_entities()
        if local_cache is None:
            local_cache = {}

        for eid, tx in rows.items():
            pt, created = ProcessedTransaction.objects.get_or_create(entry_id=eid)
            if not created:  # Another worker finished first; do not duplicate notes.
                continue
            if not is_transaction_hostile(tx, safe_entities=safe_entities, local_cache=local_cache):  # Ignore non-hostile transactions.
                continue
            flags = []
            if tx['type']:  # Skip type analysis when CCP omitted the ref type.
                for key in SUS_TYPES:
                    if key in tx['type']:  # Tag suspicious ref types for operators.
                        flags.append(f"Transaction type is **{tx['type']}**")
                if BigBrotherConfig.get_solo().show_market_transactions:
                    if "market_escrow" in tx['type'] or "market_transaction" in tx['type']:
                        flags.append(f"Transaction type is **{tx['type']}**")
            cfg = BigBrotherConfig.get_solo()

            fpid = tx.get("first_party_id")
            if get_hostile_state(fpid, 'character'):
                flags.append(f"first_party **{tx['first_party_name']}** is hostile/blacklisted")

            spid = tx.get("second_party_id")
            if get_hostile_state(spid, 'character'):
                flags.append(f"second_party **{tx['second_party_name']}** is hostile/blacklisted")

            loc_id = tx.get('location_id') or tx.get('system_id')
            if loc_id and is_location_hostile(tx.get('location_id'), tx.get('system_id')):
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

            flags_text = "\n    - ".join(flags)

            note = (
                f"- **{tx['date']}** · **{tx['amount']} ISK**"
                f"\n  - **Type:** {tx['type']}"
                f"\n  - **From:** {tx['first_party_name']} ({tx['first_party_corporation']} | {tx['first_party_alliance']})"
                f"\n  - **To:** {tx['second_party_name']} ({tx['second_party_corporation']} | {tx['second_party_alliance']})"
                f"\n  - **Reason:** {tx['reason']}"
                f"\n  - **Context:** {tx['context']}"
                f"\n  - **Flags:**"
                f"\n    - {flags_text}"
            )
            SusTransactionNote.objects.update_or_create(
                transaction=pt,
                defaults={'user_id': corp_id, 'note': note}
            )
            notes[eid] = note

    for note_obj in SusTransactionNote.objects.filter(user_id=corp_id):  # Merge previously stored notes to maintain history.
        notes[note_obj.transaction.entry_id] = note_obj.note

    return notes
