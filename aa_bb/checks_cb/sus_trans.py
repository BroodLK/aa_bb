"""
Corporate wallet journal analysis helpers mirroring the member-level checks.
"""

import html
import logging
import requests
from typing import Dict, Optional, List
from datetime import datetime, timedelta
from django.utils import timezone

from allianceauth.eveonline.models import EveCorporationInfo

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from ..app_settings import (
    get_eve_entity_type,
    get_entity_info,
    aablacklist_active,
    resolve_location_name,
    resolve_location_system_id,
    is_location_hostile,
    get_system_owner,
)

if aablacklist_active():
    from aa_bb.checks.add_to_blacklist import check_char_add_to_bl

try:
    from corptools.models import (
        CorporationAudit,
        CorporationWalletJournalEntry,
        CorporationMarketTransaction,
        Structure,
    )
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")
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

MAJOR_HUBS = {30000142, 30002187, 30002659, 30002510, 30002053}
SECONDARY_HUBS = {30002661, 30003733, 30001389, 30000144}


def is_major_hub(tx: dict) -> bool:
    system_id = tx.get("system_id")
    if not system_id:
        return False
    return int(system_id) in MAJOR_HUBS


def is_secondary_hub(tx: dict) -> bool:
    system_id = tx.get("system_id")
    if not system_id:
        return False
    return int(system_id) in SECONDARY_HUBS


def is_excluded_system(tx: dict, excluded_str: str) -> bool:
    if not excluded_str:
        return False
    system_id = tx.get("system_id")
    if not system_id:
        return False
    excluded_ids = {int(s.strip()) for s in excluded_str.split(",") if s.strip().isdigit()}
    return int(system_id) in excluded_ids


def get_or_create_prices(item_id):
    cfg = BigBrotherConfig.get_solo()

    # Check local cache first
    try:
        price_obj = EveItemPrice.objects.get(eve_type_id=item_id)
        # If it's fresh (less than configured days), return it
        if price_obj.updated > timezone.now() - timedelta(days=cfg.market_transactions_price_max_age):
            return price_obj
    except EveItemPrice.DoesNotExist:
        price_obj = None

    # Need to fetch/refresh
    primary = cfg.market_transactions_price_method
    methods = [primary]
    if primary == 'Janice':
        methods.append('Fuzzwork')
    else:
        methods.append('Janice')

    buy = None
    sell = None

    for method in methods:
        if method == 'Janice':
            api_key = cfg.market_transactions_janice_api_key
            if not api_key:
                continue
            try:
                response = requests.get(
                    f"https://janice.e-351.com/api/rest/v2/pricer/{item_id}",
                    headers={
                        "Content-Type": "text/plain",
                        "X-ApiKey": api_key,
                        "accept": "application/json",
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    if "immediatePrices" in data:
                        if cfg.market_transactions_price_instant:
                            buy = float(data["immediatePrices"]["buyPrice5DayMedian"])
                            sell = float(data["immediatePrices"]["sellPrice5DayMedian"])
                        else:
                            buy = float(data["top5AveragePrices"]["buyPrice5DayMedian"])
                            sell = float(data["top5AveragePrices"]["sellPrice5DayMedian"])
                        break
            except Exception as e:
                logger.error(f"Janice price fetch failed for {item_id}: {e}")

        elif method == 'Fuzzwork':
            station_id = cfg.market_transactions_fuzzwork_station_id or 60003760
            try:
                response = requests.get(
                    "https://market.fuzzwork.co.uk/aggregates/",
                    params={
                        "types": item_id,
                        "station": station_id,
                    },
                    timeout=10
                )
                if response.status_code == 200:
                    data = response.json()
                    if str(item_id) in data:
                        item_data = data[str(item_id)]
                        if cfg.market_transactions_price_instant:
                            buy = float(item_data["buy"]["max"])
                            sell = float(item_data["sell"]["min"])
                        else:
                            buy = float(item_data["buy"]["percentile"])
                            sell = float(item_data["sell"]["percentile"])
                        break
            except Exception as e:
                logger.error(f"Fuzzwork price fetch failed for {item_id}: {e}")

    if buy is not None and sell is not None:
        if price_obj:
            price_obj.buy = buy
            price_obj.sell = sell
            price_obj.save()
            return price_obj
        else:
            return EveItemPrice.objects.create(
                eve_type_id=item_id,
                buy=buy,
                sell=sell
            )

    return price_obj


def is_above_threshold(tx: dict, threshold_percent: float) -> bool:
    type_id = tx.get("type_id")
    amount = tx.get("raw_amount", 0)
    if not type_id or amount == 0:
        return True

    avg_price = None

    if EVEUNIVERSE_INSTALLED:
        cfg = BigBrotherConfig.get_solo()
        try:
            price_obj = EveMarketPrice.objects.filter(eve_type_id=type_id).first()
            if price_obj and price_obj.average_price and price_obj.average_price > 0:
                # Check age
                if hasattr(price_obj, 'updated_at') and price_obj.updated_at > timezone.now() - timedelta(days=cfg.market_transactions_price_max_age):
                    avg_price = float(price_obj.average_price)
        except Exception:
            logger.exception("Error checking EveUniverse price")

    if avg_price is None:
        # Fallback to local cache / Janice / Fuzzwork
        try:
            local_price = get_or_create_prices(type_id)
            if local_price:
                avg_price = (local_price.buy + local_price.sell) / 2
        except Exception:
            logger.exception("Error checking fallback prices")

    if avg_price is None or avg_price <= 0:
        return True

    try:
        quantity = tx.get("quantity", 1)
        if quantity == 0:
            quantity = 1
        unit_price = abs(amount) / quantity

        diff_percent = (abs(unit_price - avg_price) / avg_price) * 100
        if diff_percent > threshold_percent:
            return True
    except Exception:
        logger.exception("Error checking price threshold")

    return False

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
    corp_info = EveCorporationInfo.objects.get(corporation_id=corp_id)
    corp_audit = CorporationAudit.objects.get(corporation=corp_info)

    qs = CorporationWalletJournalEntry.objects.filter(division__corporation=corp_audit)
    logger.info(f"qs:{qs.count()}")
    return qs


def get_user_transactions(qs) -> Dict[int, Dict]:
    """
    Transform raw WalletJournalEntry queryset into structured dict
    with first_party (first_party) and second_party (second_party) info,
    resolving corp/alliance at transaction time.
    """
    result: Dict[int, Dict] = {}
    for entry in qs:
        tx_id = entry.entry_id
        tx_date = entry.date

        # first_party = first_party_id
        first_party_id = entry.first_party_id
        first_party_type = get_eve_entity_type(first_party_id)
        iinfo = get_entity_info(first_party_id, tx_date)

        # second_party = second_party_id
        second_party_id = entry.second_party_id
        second_party_type = get_eve_entity_type(second_party_id)
        ainfo = get_entity_info(second_party_id, tx_date)

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
            context = f"Character: {get_entity_info(context_id, tx_date)['name']}"
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
    #logger.debug(f"Transformed {len(result)} transactions")
    return result


def is_transaction_hostile(tx: dict) -> bool:
    """
    Mark transaction as hostile if first_party or second_party or corps/alliances are blacklisted
    """
    ttype = tx.get("type") or ""
    is_sus_type = any(st in ttype for st in SUS_TYPES)
    is_market = "market_escrow" in ttype or "market_transaction" in ttype

    if not (is_sus_type or is_market):
        return False

    cfg = BigBrotherConfig.get_solo()
    if aablacklist_active():
        if check_char_add_to_bl(tx.get('first_party_id')) or check_char_add_to_bl(tx.get('second_party_id')):  # Either party is on the blacklist.
            return True
    wlcorp = {s.strip() for s in (cfg.whitelist_corporations or "").split(',') if s.strip()}
    wlali = {s.strip() for s in (cfg.whitelist_alliances or "").split(',') if s.strip()}
    fpcorp = str(tx.get('first_party_corporation_id') or '')
    spcorp = str(tx.get('second_party_corporation_id') or '')
    fpali = str(tx.get('first_party_alliance_id') or '')
    spali = str(tx.get('second_party_alliance_id') or '')
    # Check if both parties are whitelisted (corp OR alliance)
    fp_whitelisted = fpcorp in wlcorp or fpali in wlali
    sp_whitelisted = spcorp in wlcorp or spali in wlali
    # logger.info(f"first party:{tx.get('first_party_id')}, cid:{fpcorp}, aid:{fpali}, fpwl:{fp_whitelisted}, 2nd: {tx.get('second_party_id')}, cid:{spcorp}, aid:{spali}, spwl:{sp_whitelisted}, wlali:{wlali}")

    if fp_whitelisted and sp_whitelisted:  # Both parties are whitelisted, so skip hostility.
        return False
    if "market_escrow" in tx.get('type') or "market_transaction" in tx.get('type'):
        if not cfg.market_transactions_show_major_hubs and is_major_hub(tx):
            return False
        if not cfg.market_transactions_show_secondary_hubs and is_secondary_hub(tx):
            return False
        if is_excluded_system(tx, cfg.market_transactions_excluded_systems):
            return False

        if cfg.market_transactions_threshold_alert and cfg.market_transactions_threshold_percent > 0:
            if not is_above_threshold(tx, cfg.market_transactions_threshold_percent):
                return False

    hostile_corps = {s.strip() for s in (cfg.hostile_corporations or "").split(",") if s.strip()}
    hostile_allis = {s.strip() for s in (cfg.hostile_alliances or "").split(",") if s.strip()}

    for key in ('first_party_corporation_id', 'second_party_corporation_id'):
        if tx.get(key) and str(tx[key]) in hostile_corps:  # Hostile corp on either side.
            return True
    for key in ('first_party_alliance_id', 'second_party_alliance_id'):
        if tx.get(key) and str(tx[key]) in hostile_allis:  # Hostile alliance on either side.
            return True

    return False


def render_transactions(corp_id: int) -> str:
    """
    Render HTML table of recent hostile wallet transactions for the corp.
    """
    qs = gather_user_transactions(corp_id)
    txs = get_user_transactions(qs)

    # sort by date desc
    all_list = sorted(txs.values(), key=lambda x: x['date'], reverse=True)
    hostile: List[dict] = []
    for tx in all_list:
        if is_transaction_hostile(tx):  # Keep only transactions that tripped hostility logic.
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

    parts = ['<table class="table table-striped">','<thead>','<tr>']
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
            if aablacklist_active():
                if col in ('first_party_name', 'second_party_name') and check_char_add_to_bl(t.get(col + '_id', -1)):  # Parties on blacklist.
                    style = 'color: red;'
            if col.endswith('corporation') and t.get(col + '_id') and str(t[col + '_id']) in hostile_corps:  # Hostile corps.
                style = 'color: red;'
            if col.endswith('alliance') and t.get(col + '_id') and str(t[col + '_id']) in hostile_allis:  # Hostile alliances.
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


def get_corp_hostile_transactions(corp_id: int) -> Dict[int, str]:
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
        for eid, tx in rows.items():
            pt, created = ProcessedTransaction.objects.get_or_create(entry_id=eid)
            if not created:  # Another worker finished first; do not duplicate notes.
                continue
            if not is_transaction_hostile(tx):  # Ignore non-hostile transactions.
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
            hostile_corps = {s.strip() for s in (cfg.hostile_corporations or "").split(",") if s.strip()}
            hostile_allis = {s.strip() for s in (cfg.hostile_alliances or "").split(",") if s.strip()}

            if aablacklist_active():
                if tx['first_party_id'] and check_char_add_to_bl(tx['first_party_id']):  # First party on blacklist.
                    flags.append(f"first_party **{tx['first_party_name']}** is on blacklist")
            if str(tx['first_party_corporation_id']) in hostile_corps:  # First-party corporation is flagged hostile.
                flags.append(f"first_party corp **{tx['first_party_corporation']}** is hostile")
            if str(tx['first_party_alliance_id']) in hostile_allis:  # First-party alliance is flagged hostile.
                flags.append(f"first_party alliance **{tx['first_party_alliance']}** is hostile")
            if aablacklist_active():
                if tx['second_party_id'] and check_char_add_to_bl(tx['second_party_id']):  # Counterparty character is hostile.
                    flags.append(f"second_party **{tx['second_party_name']}** is on blacklist")
            if str(tx['second_party_corporation_id']) in hostile_corps:  # Counterparty corporation is hostile.
                flags.append(f"second_party corp **{tx['second_party_corporation']}** is hostile")
            if str(tx['second_party_alliance_id']) in hostile_allis:  # Counterparty alliance is hostile.
                flags.append(f"second_party alliance **{tx['second_party_alliance']}** is hostile")

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
                f"- **{tx['date']}**: "
                f"\n  - amount **{tx['amount']}**, "
                f"\n  - type **{tx['type']}**, "
                f"\n  - reason **{tx['reason']}**, "
                f"\n  - context **{tx['context']}**, "
                f"\n  - from **{tx['first_party_name']}**(**{tx['first_party_corporation']}**/"
                  f"**{tx['first_party_alliance']}**), "
                f"\n  - to **{tx['second_party_name']}**(**{tx['second_party_corporation']}**/"
                  f"**{tx['second_party_alliance']}**); "
                f"\n  - flags:\n    - {flags_text}"
            )
            SusTransactionNote.objects.update_or_create(
                transaction=pt,
                defaults={'user_id': corp_id, 'note': note}
            )
            notes[eid] = note

    for note_obj in SusTransactionNote.objects.filter(user_id=corp_id):  # Merge previously stored notes to maintain history.
        notes[note_obj.transaction.entry_id] = note_obj.note

    return notes
