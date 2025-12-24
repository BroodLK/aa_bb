"""
Wallet transaction hygiene checks. These helpers normalize journal rows,
flag suspicious counterparties, and keep deduplicated notes for alerts.
"""

import html
import logging
import requests
from typing import Dict, Optional
from datetime import datetime, timedelta
from django.utils import timezone

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from ..app_settings import (
    get_user_characters,
    get_entity_info,
    get_safe_entities,
    aablacklist_active,
    resolve_location_name,
    resolve_location_system_id,
    is_location_hostile,
    get_system_owner,
)

if aablacklist_active():
    from .add_to_blacklist import check_char_add_to_bl
else:
    def check_char_corp_bl(_cid: int) -> bool:
        return False

try:
    from corptools.models import (
        CharacterWalletJournalEntry as WalletJournalEntry,
        CharacterMarketTransaction,
        Structure,
    )
except ImportError:
    logger.error("Corptools not installed, corp checks will not work.")
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


def get_or_create_prices(item_id, force_refresh=True):
    cfg = BigBrotherConfig.get_solo()

    # Check local cache first
    try:
        price_obj = EveItemPrice.objects.get(eve_type_id=item_id)
        # If it's fresh (less than configured days), return it
        if not force_refresh or price_obj.updated > timezone.now() - timedelta(days=cfg.market_transactions_price_max_age):
            return price_obj
    except EveItemPrice.DoesNotExist:
        price_obj = None

    if not force_refresh and price_obj:
        return price_obj

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
        # First attempt with force_refresh=False to avoid API hit if possible
        try:
            local_price = get_or_create_prices(type_id, force_refresh=False)
            if local_price:
                avg_price = (local_price.buy + local_price.sell) / 2

                # If we have an old price, check if it's suspicious
                if avg_price > 0:
                    quantity = tx.get("quantity", 1) or 1
                    unit_price = abs(amount) / quantity
                    diff_percent = (abs(unit_price - avg_price) / avg_price) * 100

                    # If NOT suspicious even with old price, we can skip refresh
                    # If it IS suspicious, we FORCE refresh to be sure
                    if diff_percent > threshold_percent:
                        local_price = get_or_create_prices(type_id, force_refresh=True)
                        if local_price:
                            avg_price = (local_price.buy + local_price.sell) / 2
            else:
                # No price at all, must fetch
                local_price = get_or_create_prices(type_id, force_refresh=True)
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
    ttype = tx.get("type") or ""
    is_sus_type = any(st in ttype for st in SUS_TYPES)
    is_market = "market_escrow" in ttype or "market_transaction" in ttype

    if not (is_sus_type or is_market):
        return False

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
    safe_entities = get_safe_entities()

    if aablacklist_active():
        fpid = tx.get("first_party_id")
        spid = tx.get("second_party_id")
        if (fpid and fpid not in safe_entities and check_char_add_to_bl(fpid)) or \
           (spid and spid not in safe_entities and check_char_add_to_bl(spid)):
            return True

    wlcorp = {s.strip() for s in (cfg.whitelist_corporations or "").split(",") if s.strip()}
    wlali = {s.strip() for s in (cfg.whitelist_alliances or "").split(",") if s.strip()}
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

    if "market_escrow" in (tx.get("type") or "") or "market_transaction" in (tx.get("type") or ""):
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
    for key in ("first_party_corporation_id", "second_party_corporation_id"):
        kid = tx.get(key)
        if kid and kid not in safe_entities and str(kid) in hostile_corps:
            return True
    for key in ("first_party_alliance_id", "second_party_alliance_id"):
        kid = tx.get(key)
        if kid and kid not in safe_entities and str(kid) in hostile_allis:
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
            if aablacklist_active():
                if col in ('first_party_name', 'second_party_name'):
                    pid = t.get(col + '_id', -1)
                    if pid and pid not in safe_entities and check_char_add_to_bl(pid):  # Parties on blacklist.
                        style = 'color: red;'
            if col.endswith('corporation'):
                cid = t.get(col + '_id')
                if cid and cid not in safe_entities and str(cid) in hostile_corps:  # Hostile corps.
                    style = 'color: red;'
            if col.endswith('alliance'):
                aid = t.get(col + '_id')
                if aid and aid not in safe_entities and str(aid) in hostile_allis:  # Hostile alliances.
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
                hostile_corps = {s.strip() for s in (cfg.hostile_corporations or "").split(",") if s.strip()}
                hostile_allis = {s.strip() for s in (cfg.hostile_alliances or "").split(",") if s.strip()}
                safe_entities = get_safe_entities()

                if aablacklist_active():
                    fpid = tx.get("first_party_id")
                    if fpid and fpid not in safe_entities and check_char_add_to_bl(fpid):
                        flags.append(f"first_party **{tx['first_party_name']}** is on blacklist")
                    spid = tx.get("second_party_id")
                    if spid and spid not in safe_entities and check_char_add_to_bl(spid):
                        flags.append(f"second_party **{tx['second_party_name']}** is on blacklist")

                fpcid = tx.get("first_party_corporation_id")
                if fpcid and fpcid not in safe_entities and str(fpcid) in hostile_corps:
                    flags.append(f"first_party corp **{tx['first_party_corporation']}** is hostile")
                fpaid = tx.get("first_party_alliance_id")
                if fpaid and fpaid not in safe_entities and str(fpaid) in hostile_allis:
                    flags.append(f"first_party alliance **{tx['first_party_alliance']}** is hostile")
                spcid = tx.get("second_party_corporation_id")
                if spcid and spcid not in safe_entities and str(spcid) in hostile_corps:
                    flags.append(f"second_party corp **{tx['second_party_corporation']}** is hostile")
                spaid = tx.get("second_party_alliance_id")
                if spaid and spaid not in safe_entities and str(spaid) in hostile_allis:
                    flags.append(f"second_party alliance **{tx['second_party_alliance']}** is hostile")

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

                if tx.get("context") and tx.get("context") != "None":
                    note_lines.append(f"  Context: **{tx['context']}**")

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
