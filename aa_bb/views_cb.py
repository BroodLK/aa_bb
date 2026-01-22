import html
import errno
import socket
import traceback
import json
import time

from django.contrib.auth.decorators import login_required, permission_required
from django.db import connection
from django.core.handlers.wsgi import WSGIRequest
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    StreamingHttpResponse,
    HttpResponseForbidden,
)
from django.shortcuts import render
from django.core.cache import cache
from django_celery_beat.models import PeriodicTask
from django.utils import timezone

from celery import shared_task
from celery.exceptions import Ignore

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

logger = get_extension_logger(__name__)

from aa_bb.checks_cb.hostile_assets import render_assets, get_corp_hostile_asset_locations
from aa_bb.checks_cb.sus_trans import (
    get_user_transactions,
    is_transaction_hostile,
    gather_user_transactions,
    SUS_TYPES,
)

from aa_bb.checks_cb.sus_contracts import (
    get_user_contracts,
    is_contract_row_hostile,
    get_cell_style_for_contract_row,
    gather_user_contracts,
)

from .app_settings import (
    get_user_characters, get_entity_info, get_character_id,
    resolve_corporation_name, aablacklist_active, resolve_location_name,
    corptools_active, get_hostile_state
)
from .models import BigBrotherConfig, WarmProgress

if aablacklist_active():
    from aa_bb.checks.add_to_blacklist import (
        check_char_add_to_bl,
)

try:
    if corptools_active():
        from corptools.models import Contract
    else:
        Contract = None
except ImportError:
    Contract = None



CARD_DEFINITIONS = [
    {"title": 'Assets in hostile space', "key": "sus_asset"},
    {"title": 'Suspicious Contracts', "key": "sus_contr"},
    {"title": 'Suspicious Transactions', "key": "sus_tra"},
]


from esi.models import Token
from allianceauth.eveonline.models import EveCorporationInfo

# Index view
@login_required
@permission_required("aa_bb.basic_access_cb")
def index(request: WSGIRequest):
    """Render the CorpBrother dashboard with corp dropdown options."""
    dropdown_options = []
    from .tasks_utils import format_task_name
    task_name = format_task_name('BB run regular updates')
    task = PeriodicTask.objects.filter(name=task_name).first()
    BigBrotherConfig.get_solo().is_active = True
    if not BigBrotherConfig.get_solo().is_active:  # Inactive BB -> show disabled page.
        msg = (
            "Corp Brother is currently inactive; please fill settings and enable the task"
        )
        return render(request, "aa_cb/disabled.html", {"message": msg})
    ignored_str = BigBrotherConfig.get_solo().ignored_corporations or ""
    ignored_ids = {int(s) for s in ignored_str.split(",") if s.strip().isdigit()}
    ignored_corps = EveCorporationInfo.objects.filter(
            corporation_id__in=ignored_ids).distinct()
    logger.info(f"ignored ids: {str(ignored_ids)}, corps {len(ignored_corps)}")

    if request.user.has_perm("aa_bb.full_access_cb"):  # Full-access sees every corp in the system.
        qs = EveCorporationInfo.objects.all()

    elif request.user.has_perm("aa_bb.recruiter_access_cb"):  # Recruiters only see guest-state corp tokens.
        guest_states = BigBrotherConfig.get_solo().bb_guest_states.all()
        qs = EveCorporationInfo.objects.filter(
            corporation_id__in=Token.objects.filter(
                token_type=Token.TOKEN_TYPE_CORPORATION,
                user__state__in=guest_states
            ).values_list("character__corporation_id", flat=True)  # adjust if no FK to character
        ).distinct()

    else:
        qs = None

    if qs is not None:  # Build dropdown when user has any corp visibility.
        qsa = qs.exclude(corporation_id__in=ignored_corps.values_list("corporation_id", flat=True))
        qsa = qsa.filter(
            corporationaudit__isnull=False,
        )
        dropdown_options = (
            qsa.values_list("corporation_id", "corporation_name")
              .order_by("corporation_name")
        )

    cards = list(CARD_DEFINITIONS)
    if not corptools_active():
        cards = []

    context = {
        "dropdown_options": dropdown_options,
        "CARD_DEFINITIONS": cards,
    }
    return render(request, "aa_cb/index.html", context)


# Bulk loader (fallback)
@login_required
@permission_required("aa_bb.basic_access_cb")
def load_cards(request: WSGIRequest) -> JsonResponse:
    """Legacy bulk loader that fetches every CorpBrother card for a corp."""
    corp_id = request.GET.get("option")  # now contains corporation_id
    warm_entity_cache_task.delay(corp_id)
    cards = []
    for card in CARD_DEFINITIONS:
        try:
            content, status = get_card_data(request, corp_id, card["key"])
        except Exception as e:
            logger.error(f"Error loading bulk card {card['key']} for corp {corp_id}: {e}", exc_info=True)
            content = f"<p>Error: {str(e)}</p>"
            status = False

        if content is None:
            return JsonResponse({
                "title": card["title"],
                "content": "",
                "status": status,
            })
        else:
            cards.append({
                "title":   card["title"],
                "content": content,
                "status":  status,
            })
    logger.warning("load_cards")
    return JsonResponse({"cards": cards})


def get_user_id(character_name):
    """Lookup an auth user ID from a character name."""
    if not character_name:
        return None
    character_name = str(character_name)
    try:
        ownership = CharacterOwnership.objects.select_related('user') \
            .get(character__character_name=character_name)
        return ownership.user.id
    except CharacterOwnership.DoesNotExist:
        return None

def get_card_data(request, corp_id: int, key: str):
    """Return CorpBrother card content/status pairs."""
    logger.warning("get_card_data")
    if key == "sus_asset":  # Only the asset card is currently implemented.
        content = render_assets(corp_id)
        status  = not (content and "danger" in content)

    else:
        content = "WiP"
        status  = True

    return content, status

# Single-card loader
@login_required
@permission_required("aa_bb.basic_access_cb")
def load_card(request):
    """Return a single CorpBrother card payload for the selected corp."""
    corp_id = request.GET.get("option")
    idx    = request.GET.get("index")

    if corp_id is None or idx is None:  # Both selection parameters required.
        return HttpResponseBadRequest("Missing parameters")

    try:
        idx      = int(idx)
        card_def = CARD_DEFINITIONS[idx]
    except (ValueError, IndexError):
        return HttpResponseBadRequest("Invalid card index")

    key   = card_def["key"]
    title = card_def["title"]
    logger.info(key)
    if key in ("sus_contr","sus_tra", "sus_asset"):  # Paginated cards handled elsewhere.
        # handled via paginated endpoints
        return JsonResponse({"key": key, "title": title})

    try:
        content, status = get_card_data(request, corp_id, key)
    except Exception as e:
        logger.error(f"Error loading card {key} for corp {corp_id}: {e}", exc_info=True)
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "title":   title,
        "content": content,
        "status":  status,
    })


@shared_task(bind=True, time_limit=7200)
def warm_entity_cache_task(self, user_id):
    """
    Gather mails, contracts, transactions; warm entity cache.
    Track progress in the DB via WarmProgress.
    """
    from .models import BigBrotherConfig, WarmProgress
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_active or not cfg.is_warmer_active:
        return

    if not corptools_active():
        return
    user_main = resolve_corporation_name(user_id) or str(user_id)

    # Check for existing progress entry
    try:
        progress = WarmProgress.objects.get(user_main=user_main)
    except WarmProgress.DoesNotExist:
        progress = None

    if progress and progress.total > 0:  # Abort if another job is already processing this corp.
        first_current = progress.current
        logger.info(f"[{user_main}] detected in-progress run (current={first_current}); probing…")
        time.sleep(20)

        # re-fetch to see if it's moved
        try:
            progress = WarmProgress.objects.get(user_main=user_main)
            second_current = progress.current
        except WarmProgress.DoesNotExist:
            second_current = None

        # Now *abort* if there *was* progress; otherwise continue
        if second_current != first_current:  # Progress moved, so exit to avoid duplicate run.
            logger.info(
                f"[{user_main}] progress advanced from {first_current} to {second_current}; aborting new task."
            )
            raise Ignore(f"Task for {user_main} is already running.")
        else:
            logger.info(
                f"[{user_main}] no progress in 20 s (still {first_current}); continuing with new task."
            )

    try:
        # Initialize progress record as "Scanning"
        WarmProgress.objects.update_or_create(
            user_main=user_main,
            defaults={"current": 0, "total": 1}
        )

        # Build list of (entity_id, timestamp)
        entries = []
        contracts = gather_user_contracts(user_id)
        trans = gather_user_transactions(user_id)
        candidates = []
        for c in contracts:
            issuer_id = get_character_id(c.issuer_name)
            if issuer_id:
                candidates.append((issuer_id, getattr(c, "date_issued")))
            assignee = c.assignee_id or c.acceptor_id
            if assignee:
                candidates.append((assignee, getattr(c, "date_issued")))
        for t in trans:
            if t.first_party_id:
                candidates.append((t.first_party_id, getattr(t, "date")))
            if t.second_party_id:
                candidates.append((t.second_party_id, getattr(t, "date")))

        # Normalize candidate timestamps to the hour for cache matching
        candidates = [
            (eid, ts.replace(minute=0, second=0, microsecond=0) if hasattr(ts, 'replace') else ts)
            for eid, ts in candidates
        ]
        # Deduplicate candidates
        candidates = sorted(list(set(candidates)))

        from django.db.models import Q
        from .models import EntityInfoCache

        existing = set()
        # Process in chunks to avoid hitting database query complexity limits
        CHUNK_SIZE = 500
        for i in range(0, len(candidates), CHUNK_SIZE):
            chunk = candidates[i:i + CHUNK_SIZE]
            query_filter = Q()
            for entity_id, as_of in chunk:
                query_filter |= Q(entity_id=entity_id, as_of=as_of)

            existing.update(
                EntityInfoCache.objects.filter(query_filter)
                .values_list('entity_id', 'as_of')
            )

        for candidate in candidates:
            if candidate not in existing:  # Only fetch entity info when cache lacks the tuple.
                entries.append(candidate)

        total = len(entries)
        logger.info(f"Starting warm cache for {user_main} ({total} entries)")

        if total == 0:
            logger.info(f"Warm cache for {user_main} is already up to date.")
            return 0

        # Update the progress record with real total
        WarmProgress.objects.update_or_create(
            user_main=user_main,
            defaults={"current": 0, "total": total}
        )

        # Process each entry, updating the DB record
        for idx, (eid, ts) in enumerate(entries, start=1):
            WarmProgress.objects.filter(user_main=user_main).update(current=idx)
            get_entity_info(eid, ts)

        logger.info(f"Completed warm cache for {user_main}")
        return total
    except Exception as e:
        logger.exception(f"Error in warm cache task for {user_main}: {e}")
        raise
    finally:
        # Clean up when done
        WarmProgress.objects.filter(user_main=user_main).delete()

@login_required
@permission_required("aa_bb.basic_access_cb")
def warm_cache(request):
    """
    Endpoint to kick off warming for a given corporation ID.
    Immediately registers a WarmProgress row so queued tasks also appear.
    """
    if not BigBrotherConfig.get_solo().is_warmer_active:  # Allow admins to disable heavy warm jobs.
        return JsonResponse({"error": "Warmer disabled"}, status=403)
    logger.warning(f"warm triggered")
    option  = request.GET.get("option", "")
    user_id = option
    logger.warning(f"uid2:{user_id}")
    if not user_id:  # Require a corp selection.
        return JsonResponse({"error": "Unknown account"}, status=400)

    # Pre-create progress record so queued jobs show up
    user_main = resolve_corporation_name(user_id) or str(user_id)
    WarmProgress.objects.get_or_create(
        user_main=user_main,
        defaults={"current": 0, "total": 0}
    )

    # Enqueue the celery task
    warm_entity_cache_task.delay(user_id)
    return JsonResponse({"started": True})


@login_required
@permission_required("aa_bb.basic_access_cb")
def get_warm_progress(request):
    """AJAX helper returning progress for corp cache warm jobs."""
    try:
        qs = WarmProgress.objects.all()
        users = [
            {"user": wp.user_main, "current": wp.current, "total": wp.total}
            for wp in qs
        ]
        queued_names = [wp.user_main for wp in qs if wp.current == 0]

        return JsonResponse({
            "in_progress": bool(users),
            "users": users,
            "queued": {
                "count": len(queued_names),
                "names": queued_names,
            },
        })
    except (ConnectionResetError, socket.error) as e:
        if isinstance(e, ConnectionResetError) or getattr(e, 'errno', None) == errno.ECONNRESET:
            # client disconnected — nothing to log
            return None
        raise





# Paginated endpoints for Suspicious Contracts
@login_required
@permission_required("aa_bb.basic_access_cb")
def list_contract_ids(request):
    """
    Return JSON list of all contract IDs and issue dates for the selected corporation.
    """
    option = request.GET.get("option")
    if not option:
        return JsonResponse({"error": "Missing corporation selection"}, status=400)

    qs = gather_user_contracts(option)
    if hasattr(qs, 'order_by'):
        qs = qs.order_by('-date_issued')
    qs = qs.values_list('contract_id', 'date_issued')

    contracts = [
        {'id': cid, 'date': dt.isoformat()} for cid, dt in qs
    ]
    return JsonResponse({'contracts': contracts})


@login_required
@permission_required("aa_bb.basic_access_cb")
def check_contract_batch(request):
    """
    Check a slice of contracts for hostility by start/limit parameters.
    Returns JSON with `checked` count and list of `hostile_found`,
    each entry including a `cell_styles` dict for inline styling.
    """
    option = request.GET.get("option")
    start  = int(request.GET.get("start", 0))
    limit  = int(request.GET.get("limit", 10))
    if not option:
        return JsonResponse({"error": "Missing corporation selection"}, status=400)

    # 1) Ensure the full QuerySet is available
    cache_key = f"corp_contract_qs_{option}"
    qs_all = cache.get(cache_key)
    if qs_all is None:
        qs_all = gather_user_contracts(option)
        cache.set(cache_key, qs_all, 300)

    # 2) Slice out just this batch of model instances
    batch_qs = qs_all[start:start + limit]

    # 3) Hydrate only this batch
    batch_map = get_user_contracts(batch_qs)

    HIDDEN = {
        'assignee_alliance_id', 'assignee_corporation_id',
        'issuer_alliance_id', 'issuer_corporation_id',
        'assignee_id', 'issuer_id', 'contract_id'
    }

    hostile = []
    for cid, row in batch_map.items():
        if is_contract_row_hostile(row):  # Only emit rows that match hostile heuristics.
            # build style map for visible columns
            style_map = {
                col: get_cell_style_for_contract_row(col, row)
                for col in row
                if col not in HIDDEN
            }
            # package only the visible fields + styles
            payload = {col: row[col] for col in row if col not in HIDDEN}
            payload['cell_styles'] = style_map
            hostile.append(payload)

    return JsonResponse({
        'checked': len(batch_qs),
        'hostile_found': hostile
    })




@login_required
@permission_required("aa_bb.basic_access_cb")
def stream_contracts_sse(request: WSGIRequest):
    """Push suspicious corp contracts over SSE for the recruiter dashboard."""
    option = request.GET.get("option", "")
    user_id = option
    if not user_id:  # Require a corp identifier.
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    try:
        qs    = gather_user_contracts(user_id)
        total = qs.count() if hasattr(qs, 'count') else len(qs)
        connection.close()
    except Exception as e:
        logger.error(f"Error initializing contract stream for {option}: {e}", exc_info=True)
        return HttpResponseBadRequest(f"Error loading contracts: {str(e)}")

    def generator():
        try:
            # Initial SSE heartbeat
            yield ": ok\n\n"
            processed = hostile_count = 0

            if total == 0:  # Immediately finish if the queryset is empty.
                # Notify client that streaming completed without hostile entries
                yield "event: done\ndata:0\n\n"
                return

            headers = [
                "issued_date", "end_date",
                "contract_type", "issuer_name", "issuer_corporation",
                "issuer_alliance", "assignee_name", "assignee_corporation",
                "assignee_alliance", "status", "start_location", "end_location",
            ]
            header_html = (
                "<tr>" +
                "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in headers) +
                "</tr>"
            )
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            batch_size = 50
            for i in range(0, total, batch_size):
                batch = qs[i : i + batch_size]
                rows_map = get_user_contracts(batch)
                # Sort keys to maintain some order; date desc is preferred
                sorted_keys = sorted(rows_map.keys(), key=lambda k: rows_map[k]['issued_date'] or timezone.now(), reverse=True)

                for cid in sorted_keys:
                    row = rows_map[cid]
                    processed += 1
                    # Ping to keep connection alive
                    yield ": ping\n\n"

                    try:
                        style_map = {
                            col: get_cell_style_for_contract_row(col, row)
                            for col in row
                        }
                        yield ": ping\n\n"
                        row['cell_styles'] = style_map

                        if is_contract_row_hostile(row):  # Emit only hostile rows.
                            hostile_count += 1
                            tr_html = _render_contract_row_html(row)
                            yield f"event: contract\ndata:{json.dumps(tr_html)}\n\n"

                        # Progress update
                        yield (
                            "event: progress\n"
                            f"data:{processed},{total},{hostile_count}\n\n"
                        )
                    except (ConnectionResetError, BrokenPipeError):
                        return
                    except Exception:
                        logger.exception(f"Error while processing contract {cid}")
                        continue

                connection.close()

            # Done
            yield "event: done\ndata:bye\n\n"

        except (ConnectionResetError, BrokenPipeError):
            logger.debug("Client disconnected from contract SSE (outer)")
            return
        except Exception as e:
            logger.error(f"Error in contract stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"
            return

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@permission_required("aa_bb.basic_access_cb")
def stream_assets_sse(request):
    """Stream hostile assets for a corporation via SSE."""
    corp_id = request.GET.get("option", "")
    if not corp_id:
        return HttpResponseBadRequest("Missing corp_id")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    def generator():
        try:
            yield ": ok\n\n"
            systems = get_corp_hostile_asset_locations(corp_id)
            total = len(systems)
            processed = hostile_count = 0

            if total == 0:
                yield "event: done\ndata:0\n\n"
                return

            headers = ["System", "Location", "Owner", "Region"]
            header_html = "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            now_ts = timezone.now()

            for system_id, data in systems.items():
                processed += 1
                system_name = data.get("name") or f"ID {system_id}"
                owner = data.get("owner", "Unresolvable")
                region = data.get("region", "Unknown Region")

                for rec in data.get("records", []):
                    hostile_count += 1
                    loc_name = rec.get("location_name", "Unknown Location")
                    tr = f"<tr><td>{html.escape(system_name)}</td><td>{html.escape(loc_name)}</td><td><span class='text-danger'>{html.escape(owner)}</span></td><td>{html.escape(region)}</td></tr>"
                    yield f"event: asset\ndata:{json.dumps(tr)}\n\n"

                yield f"event: progress\ndata:{processed},{total},{hostile_count}\n\n"

            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in corp asset stream for {corp_id}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


VISIBLE = [
    "sent_date", "subject",
    "sender_name", "sender_corporation", "sender_alliance",
    "recipient_names", "recipient_corps", "recipient_alliances",
    "content", "status",
]

VISIBLE_CONTR = [
    "issued_date", "end_date",
    "contract_type", "issuer_name", "issuer_corporation",
    "issuer_alliance", "assignee_name", "assignee_corporation",
    "assignee_alliance", "status", "start_location", "end_location",
]

def _render_contract_row_html(row: dict) -> str:
    """
    Render one hostile contract row, applying inline styles
    from row['cell_styles'] *or* from any hidden-ID–based flags.
    """
    cells = []

    # for any visible header like "issuer_name", map its ID column:
    def id_for(col):
        if col.endswith("_name"):
            return col[:-5] + "_id"
        elif col.endswith("_corporation"):
            return col[:-12] + "_corporation_id"
        elif col.endswith("_alliance"):
            return col[:-9] + "_alliance_id"
        return None

    style_map = row.get('cell_styles', {})

    for col in VISIBLE_CONTR:
        val   = row.get(col, "")
        text  = html.escape(str(val))

        # first, try the direct style:
        style = style_map.get(col, "") or ""

        # render the cell
        if style:
            cells.append(f'<td style="{style}">{text}</td>')
        else:
            cells.append(f'<td>{text}</td>')

    return "<tr>" + "".join(cells) + "</tr>"


@login_required
@permission_required("aa_bb.basic_access_cb")
def stream_transactions_sse(request):
    """
    Stream hostile wallet‐transactions one <tr> at a time via SSE,
    hydrating first‐ and second‐party info on the fly.
    """
    option  = request.GET.get("option", "")
    user_id = option
    if not user_id:  # Need a corp selection for SSE.
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    cfg = BigBrotherConfig.get_solo()
    ref_types = list(SUS_TYPES)
    if cfg.show_market_transactions:
        ref_types.extend(["market_escrow", "market_transaction"])

    try:
        qs = gather_user_transactions(user_id, ref_types=ref_types)
        if hasattr(qs, 'order_by'):
            qs = qs.order_by('-date')
        total = qs.count() if hasattr(qs, 'count') else len(qs)
        connection.close()
        if total == 0:  # No transactions -> return SSE stream with no data message
            def empty_generator():
                yield ": ok\n\n"
                yield "event: message\ndata:No transactions found.\n\n"
                yield "event: done\ndata:bye\n\n"
            resp = StreamingHttpResponse(empty_generator(), content_type='text/event-stream')
            resp["Cache-Control"] = "no-cache"
            resp["X-Accel-Buffering"] = "no"
            return resp

        # Determine headers from a single hydrated row (after empty check)
        sample_map = get_user_transactions(qs[:1])
        sample_row = next(iter(sample_map.values()), None)
        HIDDEN        = {
            'first_party_id','second_party_id',
            'first_party_corporation_id','second_party_corporation_id',
            'first_party_alliance_id','second_party_alliance_id',
            'entry_id'
        }
        if sample_row:  # Derive headers from real data when available.
            headers = [h for h in sample_row.keys() if h not in HIDDEN]
        else:
            # Fallback to a safe default when sampling finds nothing
            headers = [
                'date', 'amount', 'balance', 'description', 'reason',
                'first_party_name', 'first_party_corporation', 'first_party_alliance',
                'second_party_name', 'second_party_corporation', 'second_party_alliance',
                'context', 'type',
            ]
    except Exception as e:
        logger.error(f"Error in stream_transactions_sse setup: {e}", exc_info=True)
        return HttpResponseBadRequest(f"Error loading transactions: {str(e)}")

    def generator():
        try:
            yield ": ok\n\n"                # initial heartbeat
            processed = hostile_count = 0

            # Emit table header row once
            header_html = (
                "<tr>" +
                "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in headers) +
                "</tr>"
            )
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            cfg = BigBrotherConfig.get_solo()

            batch_size = 100
            for i in range(0, total, batch_size):
                batch = qs[i:i + batch_size]
                rows_map = get_user_transactions(batch)
                # Sort keys to maintain some order if possible, though date desc is better
                sorted_keys = sorted(rows_map.keys(), key=lambda k: rows_map[k]['date'], reverse=True)

                for eid in sorted_keys:
                    row = rows_map[eid]
                    processed += 1
                    if processed % 10 == 0:
                        yield ": ping\n\n"         # keep‐alive

                    if is_transaction_hostile(row):  # Only push rows that meet hostility rules.
                        hostile_count += 1

                        # build the <tr> using same style logic as render_transactions()
                        cells = []
                        for col in headers:
                            val = row.get(col, "")
                            text = html.escape(str(val))
                            style = ""
                            # type‐based red
                            if col == 'type':
                                if any(st in row['type'] for st in SUS_TYPES):
                                    style = 'color:red;'
                                if cfg.show_market_transactions:
                                    if "market_escrow" in row['type'] or "market_transaction" in row['type']:
                                        style = 'color:red;'
                            # first/second party name
                            if col in ('first_party_name','second_party_name'):
                                pid = row[col.replace("_name", "_id")]
                                if get_hostile_state(pid, 'character', when=row['date']):
                                    style = 'color:red;'
                            # corps & alliances
                            if col.endswith('corporation'):
                                cid = row[f"{col}_id"]
                                if cid and get_hostile_state(cid, 'corporation', when=row['date']):
                                    style = 'color:red;'
                            if col.endswith('alliance'):
                                aid = row[f"{col}_id"]
                                if aid and get_hostile_state(aid, 'alliance', when=row['date']):
                                    style = 'color:red;'
                            def make_td(text, style=""):
                                style_attr = f' style="{style}"' if style else ""
                                return f"<td{style_attr}>{text}</td>"
                            cells.append(make_td(text, style))
                        tr_html = "<tr>" + "".join(cells) + "</tr>"
                        yield f"event: transaction\ndata:{json.dumps(tr_html)}\n\n"

                    # progress update every few rows to avoid flood
                    if processed % 10 == 0 or processed == total:
                        yield (
                            "event: progress\n"
                            f"data:{processed},{total},{hostile_count}\n\n"
                        )
                connection.close()

            # Done
            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in transaction stream generator: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(f'Stream error: {str(e)}')}\n\n"



    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
