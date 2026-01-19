import html
import time
import json
import gc

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.db import connection
from django.core.handlers.wsgi import WSGIRequest
from django.db.utils import OperationalError, ProgrammingError
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    StreamingHttpResponse,
    HttpResponseForbidden,
)
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.core.cache import cache
from django.db.models import Q
from django_celery_beat.models import PeriodicTask
from django.utils import timezone

from celery import shared_task
from celery.exceptions import Ignore

from allianceauth.authentication.models import UserProfile, CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from .forms import LeaveRequestForm
from .app_settings import (
    get_user_characters, get_entity_info, get_main_character_name,
    get_character_id, get_pings, aablacklist_active, send_status_embed,
    resolve_location_name, afat_active, discordbot_active, corptools_active,
    get_hostile_state, get_safe_entities, is_safe_entity
)
from .models import BigBrotherConfig, WarmProgress, LeaveRequest, ComplianceTicket, ComplianceTicketComment

from aa_bb.checks.awox import render_awox_kills_html, fetch_awox_kills
from aa_bb.checks.corp_changes import get_frequent_corp_changes
from aa_bb.checks.cyno import render_user_cyno_info_html
from aa_bb.checks.hostile_assets import render_assets, get_asset_locations
from aa_bb.checks.hostile_clones import render_clones, get_clones
from aa_bb.checks.coalition_blacklist import get_external_blacklist_link
from aa_bb.checks.alliance_blacklist import get_alliance_blacklist_link
from aa_bb.checks.sus_contacts import render_contacts, get_user_contacts
from aa_bb.checks.sus_mails import (
    is_mail_row_hostile,
    get_cell_style_for_mail_cell,
    gather_user_mails,
    get_user_mails,
    render_mails,
)
from aa_bb.checks.sus_trans import (
    get_user_transactions,
    is_transaction_hostile,
    gather_user_transactions,
    render_transactions,
    SUS_TYPES,
)
from .views_cb import CARD_DEFINITIONS

if aablacklist_active():
    from aa_bb.checks.add_to_blacklist import (
        get_add_to_blacklist_html,
        add_user_characters_to_blacklist,
        check_char_add_to_bl,
    )

from aa_bb.checks.sus_contracts import (
    get_user_contracts,
    is_contract_row_hostile,
    get_cell_style_for_contract_row,
    gather_user_contracts,
)
from aa_bb.checks.roles_and_tokens import render_user_roles_tokens_html
from aa_bb.checks.clone_state import render_character_states_html
from aa_bb.checks.skills import render_user_skills_html

logger = get_extension_logger(__name__)

try:
    if corptools_active():
        from corptools.models import Contract, CharacterAudit
    else:
        Contract = None
        CharacterAudit = None
except ImportError:
    Contract = None
    CharacterAudit = None


def get_allowed_alliance_id():
    cfg = BigBrotherConfig.get_solo()
    if not cfg.member_alliances:
        return None
    return int(cfg.member_alliances.split(",")[0].strip())


def get_allowed_coalition_alliance_ids():
    cfg = BigBrotherConfig.get_solo()
    if not cfg.whitelist_alliances:
        return set()

    return {
        int(a.strip())
        for a in cfg.whitelist_alliances.split(",")
        if a.strip().isdigit()
    }
try:
    ALLOWED_ALLIANCE_ID = get_allowed_alliance_id()
    ALLOWED_COALITION_ALLIANCE_IDS = get_allowed_coalition_alliance_ids()
except (OperationalError, ProgrammingError):
    ALLOWED_ALLIANCE_ID = None

CARD_DEFINITIONS = []

if aablacklist_active():
    CARD_DEFINITIONS.append(
        {"title": 'Add User to Blacklist', "key": "corp_bl"}
    )

CARD_DEFINITIONS += [
    {"title": 'Alliance Blacklist', "key": "alliance_bl"},
    {"title": 'Coalition Blacklist', "key": "external_bl"},
    {"title": 'Audit Compliance', "key": "compliance"},
    {"title": 'Player Corp History', "key": "freq_corp"},
    {"title": 'AWOX Kills', "key": "awox"},
    {"title": 'Omega State', "key": "clone_states"},
    {"title": 'Jump Clones', "key": "sus_clones"},
    {"title": 'Assets In Hostile Space', "key": "sus_asset"},
    {"title": 'Suspicious Contacts', "key": "sus_conta"},
    {"title": 'Suspicious Contracts', "key": "sus_contr"},
    {"title": 'Suspicious Mails', "key": "sus_mail"},
    {"title": 'Suspicious Transactions', "key": "sus_tra"},
    {"title": 'Cyno?', "key": "cyno"},
    {"title": 'Skills', "key": "skills"},
]



def get_available_cards():
    """Return card configurations filtered by settings and permissions."""
    cards = list(CARD_DEFINITIONS)
    try:
        cfg = BigBrotherConfig.get_solo()
    except (BigBrotherConfig.DoesNotExist, OperationalError, ProgrammingError):
        return cards

    if not corptools_active():
        corptools_cards = {
            "compliance", "freq_corp", "awox", "clone_states", "sus_clones",
            "sus_asset", "sus_conta", "sus_contr", "sus_mail", "sus_tra",
            "cyno", "skills"
        }
        cards = [card for card in cards if card["key"] not in corptools_cards]

    if not cfg.alliance_blacklist_url:
        cards = [card for card in cards if card["key"] != "alliance_bl"]

    if not cfg.external_blacklist_url:
        cards = [card for card in cards if card["key"] != "external_bl"]

    if not aablacklist_active():
        cards = [card for card in cards if card["key"] != "corp_bl"]

    return cards


def get_user_id(character_name):
    """Resolve an auth user ID from a character name, respecting member restrictions."""
    if not character_name:
        return None
    character_name = str(character_name)
    try:
        ownership = CharacterOwnership.objects.select_related('user__profile__main_character') \
            .get(character__character_name=character_name)

        return ownership.user.id
    except CharacterOwnership.DoesNotExist:
        return None

# Single-card loader
@login_required
@permission_required("aa_bb.basic_access")
def load_card(request):
    """Return the rendered HTML for a single dashboard card."""
    option = request.GET.get("option")
    idx    = request.GET.get("index")
    cards = get_available_cards()

    if option is None or idx is None:  # Card fetches require both parameters.
        return HttpResponseBadRequest("Missing parameters")

    try:
        idx      = int(idx)
        card_def = cards[idx]
    except (ValueError, IndexError):
        return HttpResponseBadRequest("Invalid card index")

    key   = card_def["key"]
    title = card_def["title"]
    logger.info(key)
    if key in ("sus_contr", "sus_mail","sus_tra", "sus_asset", "sus_clones", "sus_conta"):  # Paginated cards handled separately via SSE/ajax.
        # handled via paginated endpoints
        return JsonResponse({"key": key, "title": title})

    target_user_id = get_user_id(option)
    if target_user_id is None:  # Unknown character selection.
        return JsonResponse({"error": "Unknown account"}, status=404)

    try:
        content, status = get_card_data(request, target_user_id, key)
    except Exception as e:
        logger.error(f"Error loading card {key} for {option}: {e}", exc_info=True)
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({
        "title":   title,
        "content": content,
        "status":  status,
    })


# Bulk loader (fallback)
@login_required
@permission_required("aa_bb.basic_access")
def load_cards(request: WSGIRequest) -> JsonResponse:
    """Bulk-load every card for a selected user (fallback for legacy UI)."""
    selected_option = request.GET.get("option")
    user_id = get_user_id(selected_option)
    warm_entity_cache_task.delay(user_id)
    cards = []
    for card in get_available_cards():
        try:
            content, status = get_card_data(request, user_id, card["key"])
        except Exception as e:
            logger.error(f"Error loading bulk card {card['key']} for {selected_option}: {e}", exc_info=True)
            content = f"<p>Error: {str(e)}</p>"
            status = False

        cards.append({
            "title":   card["title"],
            "content": content,
            "status":  status,
        })
    return JsonResponse({"cards": cards})

@shared_task(bind=True, time_limit=7200)
def warm_entity_cache_task(self, user_id, user_main=None):
    """
    Gather mails, contracts, transactions; warm entity cache.
    Track progress in the DB via WarmProgress.
    """
    from .models import BigBrotherConfig, WarmProgress
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_active or not cfg.is_warmer_active:
        return

    if user_main is None:
        user_main = get_main_character_name(user_id) or str(user_id)

    # Check for existing progress entry
    try:
        progress = WarmProgress.objects.get(user_main=user_main)
    except WarmProgress.DoesNotExist:
        progress = None

    # Determine if an existing warm job is currently making progress.
    if progress and progress.total > 0:
        first_current = progress.current
        logger.info(f"[{user_main}] detected in-progress run (current={first_current}); probing…")
        time.sleep(20)

        # Re-fetch progress record to see if current count has increased.
        try:
            progress = WarmProgress.objects.get(user_main=user_main)
            second_current = progress.current
        except WarmProgress.DoesNotExist:
            second_current = None

        # Abort if progress was detected; otherwise continue with the new task.
        if second_current != first_current:
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
            defaults={"current": 0, "total": 1} # total=1 to avoid 0/0
        )

        # Build list of (entity_id, timestamp)
        entries = []
        fetch_awox_kills(user_id, force_refresh=True)
        contracts = gather_user_contracts(user_id)
        trans = gather_user_transactions(user_id)
        mails = gather_user_mails(user_id)

        # New: also fetch assets and clones systems/stations
        from .checks.hostile_assets import get_asset_locations
        from .checks.hostile_clones import get_clones
        assets = get_asset_locations(user_id)
        clones = get_clones(user_id)

        candidates = []
        for c in contracts:
            issuer_id = get_character_id(c.issuer_name)
            if issuer_id:
                candidates.append((issuer_id, getattr(c, "date_issued")))
            assignee = c.assignee_id or c.acceptor_id
            if assignee:
                candidates.append((assignee, getattr(c, "date_issued")))
        for m in mails:
            if m.from_id:
                candidates.append((m.from_id, getattr(m, "timestamp")))
            for mr in m.recipients.all():
                if mr.recipient_id:
                    candidates.append((mr.recipient_id, getattr(m, "timestamp")))
        for t in trans:
            if t.first_party_id:
                candidates.append((t.first_party_id, getattr(t, "date")))
            if t.second_party_id:
                candidates.append((t.second_party_id, getattr(t, "date")))

        # Add systems and stations to candidates
        now_ts = timezone.now()
        for sys_id in assets.keys():
            candidates.append((sys_id, now_ts))
            for loc_id in assets[sys_id].get("locations", {}).keys():
                if loc_id:
                    candidates.append((loc_id, now_ts))

        for sys_id in clones.keys():
            candidates.append((sys_id, now_ts))
            for loc_id in clones[sys_id].get("locations", {}).keys():
                if loc_id:
                    candidates.append((loc_id, now_ts))

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
@permission_required("aa_bb.basic_access")
def warm_cache(request):
    """
    Endpoint to kick off warming for a given character name (option).
    Immediately registers a WarmProgress row so queued tasks also appear.
    """
    if not BigBrotherConfig.get_solo().is_warmer_active:  # Allow admins to disable the warmer
        return JsonResponse({"error": "Warmer disabled"}, status=403)
    option  = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:  # Invalid character selection.
        return JsonResponse({"error": "Unknown account"}, status=400)

    # Pre-create progress record so queued jobs show up
    user_main = option or get_main_character_name(user_id) or str(user_id)
    WarmProgress.objects.get_or_create(
        user_main=user_main,
        defaults={"current": 0, "total": 0}
    )

    # Enqueue the celery task
    warm_entity_cache_task.delay(user_id, user_main=user_main)
    return JsonResponse({"started": True})


@login_required
@permission_required("aa_bb.basic_access")
def get_warm_progress(request):
    """
    AJAX endpoint returning all in-flight and queued warm-up info:
      {
        in_progress: bool,
        users: [ { user, current, total }, … ],
        queued: { count, names: [...] }
      }
    """
    qs = WarmProgress.objects.all()
    users = [
        {"user": wp.user_main, "current": wp.current, "total": wp.total}
        for wp in qs
    ]
    # Those still at current == 0 are queued/not yet started
    queued_names = [wp.user_main for wp in qs if wp.current == 0]

    #logger.debug(f"get_warm_progress → users={users}, queued={queued_names}")
    return JsonResponse({
        "in_progress": bool(users),
        "users": users,
        "queued": {
            "count": len(queued_names),
            "names": queued_names,
        },
    })

# Index view
@login_required
@permission_required("aa_bb.basic_access")
def index(request: WSGIRequest):
    """Render the dashboard shell plus dropdown options for authorized recruiters."""
    dropdown_options = []
    from .tasks_utils import format_task_name
    task_name = format_task_name('BB run regular updates')
    task = PeriodicTask.objects.filter(name=task_name).first()
    cfg = BigBrotherConfig.get_solo()
    cfg.is_active = True
    if not cfg.is_active:  # Guard against misconfigured BB.
        msg = (
            "Big Brother is currently inactive; please fill settings and enable the task"
        )
        return render(request, "aa_bb/disabled.html", {"message": msg})

    member_states = cfg.bb_member_states.all()
    guest_states = cfg.bb_guest_states.all()
    if request.user.has_perm("aa_bb.full_access"):  # Full-access sees member and guest states.
        qs = UserProfile.objects.filter(state__in=member_states | guest_states).exclude(main_character=None)
    elif request.user.has_perm("aa_bb.recruiter_access"):  # Recruiters see only guest states.
        qs = UserProfile.objects.filter(state__in=guest_states).exclude(main_character=None)
    else:
        qs = None

    if qs is not None:  # Build dropdown choices only when the viewer has visibility.
        if cfg.hide_unaudited_users:
            if CharacterAudit:
                # Hide users who have no characters with an entry in corptools.audits
                audited_user_ids = CharacterAudit.objects.values_list(
                    'character__character_ownership__user_id', flat=True
                ).distinct()
                qs = qs.filter(user_id__in=audited_user_ids)
            else:
                qs = qs.filter(user__userstatus__baseline_initialized=True)

        dropdown_options = (
            qs.values_list("main_character__character_name", flat=True)
              .order_by("main_character__character_name")
        )

    context = {
        "dropdown_options": dropdown_options,
        "CARD_DEFINITIONS": get_available_cards(),
    }
    return render(request, "aa_bb/index.html", context)




# Paginated endpoints for Suspicious Contracts
@login_required
@permission_required("aa_bb.basic_access")
def list_contract_ids(request):
    """
    Return JSON list of all contract IDs and issue dates for the selected user.
    """
    option = request.GET.get("option")
    user_id = get_user_id(option)
    if user_id is None:  # Target selection must map to a known auth user.
        return JsonResponse({"error": "Unknown account"}, status=404)

    user_chars = get_user_characters(user_id)
    if Contract is not None:
        qs = Contract.objects.filter(
            character__character__character_id__in=user_chars
        ).order_by('-date_issued').values_list('contract_id', 'date_issued')
    else:
        qs = []

    contracts = [
        {'id': cid, 'date': dt.isoformat()} for cid, dt in qs
    ]
    return JsonResponse({'contracts': contracts})


@login_required
@permission_required("aa_bb.basic_access")
def check_contract_batch(request):
    """
    Check a slice of contracts for hostility by start/limit parameters.
    Returns JSON with `checked` count and list of `hostile_found`,
    each entry including a `cell_styles` dict for inline styling.
    Now uses gather_user_contracts + get_user_contracts(qs) on the full set.
    """
    option = request.GET.get("option")
    start  = int(request.GET.get("start", 0))
    limit  = int(request.GET.get("limit", 10))
    user_id = get_user_id(option)
    if user_id is None:  # Unknown selection -> 404.
        return JsonResponse({"error": "Unknown account"}, status=404)

    # 1) Ensure the full QuerySet is available
    cache_key = f"contract_qs_{user_id}"
    qs_all = cache.get(cache_key)
    if qs_all is None:  # Cache miss, gather the entire contract queryset now.
        qs_all = gather_user_contracts(user_id)
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
        if is_contract_row_hostile(row):  # Only return rows flagged as hostile.
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
@permission_required("aa_bb.basic_access")
def stream_contracts_sse(request: WSGIRequest):
    """Push suspicious contract rows to the browser using server-sent events."""
    option = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:  # SSE requires a valid user context.
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

            if total == 0:  # Nothing to scan, emit done immediately.
                # Notify client that processing completed with zero hostile hits
                yield "event: done\ndata:0\n\n"
                return

            header_html = "<tr>" + "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in VISIBLE_CONTR) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            batch_size = 50
            for i in range(0, total, batch_size):
                batch = qs[i : i + batch_size]
                contracts_map = get_user_contracts(batch)

                # Sort them if possible, by issued_date desc
                sorted_keys = sorted(contracts_map.keys(), key=lambda k: contracts_map[k]['issued_date'], reverse=True)

                for cid in sorted_keys:
                    row = contracts_map[cid]
                    processed += 1
                    if processed % 5 == 0:
                        yield ": ping\n\n"

                    style_map = {
                        col: get_cell_style_for_contract_row(col, row)
                        for col in row
                    }
                    row['cell_styles'] = style_map

                    if is_contract_row_hostile(row):  # Emit rows that match hostile heuristics.
                        hostile_count += 1
                        tr_html = _render_contract_row_html(row)
                        yield f"event: contract\ndata:{json.dumps(tr_html)}\n\n"

                    # Progress update
                    if processed % 5 == 0 or processed == total:
                        yield (
                            "event: progress\n"
                            f"data:{processed},{total},{hostile_count}\n\n"
                        )
                connection.close()

            # Done
            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in contract stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"]     = "no-cache"
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
        if col.endswith("_name"):  # Visible name columns pair with *_id fields.
            return col[:-5] + "_id"
        elif col.endswith("_corporation"):  # Map corp label to corp_id.
            return col[:-12] + "_corporation_id"
        elif col.endswith("_alliance"):  # Map alliance label to alliance_id.
            return col[:-9] + "_alliance_id"
        return None

    style_map = row.get('cell_styles', {})

    for col in VISIBLE_CONTR:
        val   = row.get(col, "")
        text  = html.escape(str(val))

        # first, try the direct style:
        style = style_map.get(col, "") or ""

        # render the cell
        if style:  # Inline styles highlight hostile issuers/assignees.
            cells.append(f'<td style="{style}">{text}</td>')
        else:
            cells.append(f'<td>{text}</td>')

    return "<tr>" + "".join(cells) + "</tr>"

def _render_mail_row_html(row: dict) -> str:
    """
    Render a single hostile mail row as <tr>…</tr> using only VISIBLE columns,
    applying red styling to any name whose ID is hostile.
    """
    cells = []
    cfg = BigBrotherConfig.get_solo()

    for col in VISIBLE:
        val = row.get(col, "")
        # recipients come as lists
        if isinstance(val, list):  # Expand recipient arrays to comma-separated spans.
            spans = []
            for i, item in enumerate(val):
                style = ""
                if col == "recipient_names":  # Hostile recipients get red styling.
                    rid = row["recipient_ids"][i]
                    if aablacklist_active():
                        if check_char_add_to_bl(rid):
                            style = "color:red;"
                elif col == "recipient_corps":  # Hostile corps -> red label.
                    cid = row["recipient_corp_ids"][i]
                    if cid and str(cid) in cfg.hostile_corporations:
                        style = "color:red;"
                elif col == "recipient_alliances":  # Hostile alliances -> red label.
                    aid = row["recipient_alliance_ids"][i]
                    if aid and str(aid) in cfg.hostile_alliances:
                        style = "color:red;"
                span = (
                    f'<span style="{style}">{html.escape(str(item))}</span>'
                    if style else
                    f'<span>{html.escape(str(item))}</span>'
                )
                spans.append(span)
            cell_html = ", ".join(spans)
        else:
            # single-valued columns: subject, content, sender_*
            style = ""
            if col.startswith("sender_"):  # Sender cells use existing cell style helper.
                style = get_cell_style_for_mail_cell(col, row, None)
            if col == "sender_name":
                for key in ["GM ","CCP "]:
                    if key in str(row["sender_name"]):  # Highlight official senders (GM/CCP) in red to stand out.
                        style = "color:red;"
            if style:  # Apply span styling when a highlight was requested.
                cell_html = f'<span style="{style}">{html.escape(str(val))}</span>'
            else:
                cell_html = html.escape(str(val))
        cells.append(f"<td>{cell_html}</td>")

    return "<tr>" + "".join(cells) + "</tr>"

@login_required
@permission_required("aa_bb.basic_access")
def stream_mails_sse(request):
    """Stream hostile mails one row at a time via SSE, hydrating sender+recipients."""
    option  = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:  # Clients must specify a valid account to inspect.
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    try:
        qs    = gather_user_mails(user_id)
        total = qs.count()
        connection.close()
    except Exception as e:
        logger.error(f"Error initializing mail stream for {option}: {e}", exc_info=True)
        return HttpResponseBadRequest(f"Error loading mails: {str(e)}")

    def generator():
        try:
            # initial SSE heartbeat
            yield ": ok\n\n"
            processed = hostile_count = 0

            if total == 0:  # Nothing to stream -> immediately finish.
                # Notify client that streaming finished without hostile mails
                yield "event: done\ndata:0\n\n"
                return

            header_html = "<tr>" + "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in VISIBLE) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            batch_size = 50
            for i in range(0, total, batch_size):
                batch = qs[i : i + batch_size]
                mails_map = get_user_mails(batch)

                # Sort them if possible, by sent_date desc
                sorted_keys = sorted(mails_map.keys(), key=lambda k: mails_map[k]['sent_date'], reverse=True)

                for mid in sorted_keys:
                    row = mails_map[mid]
                    processed += 1
                    if processed % 5 == 0:
                        yield ": ping\n\n"

                    # check hostility and, if hostile, stream the <tr>
                    if is_mail_row_hostile(row):  # Emit only hostile mail rows.
                        hostile_count += 1
                        tr = _render_mail_row_html(row)
                        yield f"event: mail\ndata:{json.dumps(tr)}\n\n"

                    # final per-mail progress
                    if processed % 5 == 0 or processed == total:
                        yield (
                            "event: progress\n"
                            f"data:{processed},{total},{hostile_count}\n\n"
                        )
                connection.close()

            # done
            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in mail stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(),
                                 content_type="text/event-stream")
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@permission_required("aa_bb.basic_access")
def stream_transactions_sse(request):
    """
    Stream hostile wallet‐transactions one <tr> at a time via SSE,
    hydrating first‐ and second‐party info on the fly.
    """
    option  = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:  # Reject SSE connection when the pilot is unknown.
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
            # Journal entries are best viewed in descending date order
            qs = qs.order_by('-date')
        total = qs.count() if hasattr(qs, 'count') else len(qs)
        logger.info(f"Transaction stream for {option}: found {total} total transactions (filtered by {ref_types})")
        connection.close()
    except Exception as e:
        logger.error(f"Error initializing transaction stream for {option}: {e}", exc_info=True)
        return HttpResponseBadRequest(f"Error loading transactions: {str(e)}")

    # Hidden columns for the transactions table
    HIDDEN        = {
        'first_party_id','second_party_id',
        'first_party_corporation_id','second_party_corporation_id',
        'first_party_alliance_id','second_party_alliance_id',
        'entry_id',
        'info_cache',
        'raw_amount',
        'system_id', 'location_id', 'type_id', 'quantity'
    }

    def generator():
        try:
            yield ": ok\n\n"                # initial heartbeat
            processed = hostile_count = 0

            if total == 0:  # No transactions -> stop immediately.
                yield "event: done\ndata:0\n\n"
                return
        except Exception as e:
            logger.error(f"Error in transaction stream initialization: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"
            return

        try:
            # Determine headers from a single hydrated row
            sample_map = get_user_transactions(qs[:1])
            sample_row = next(iter(sample_map.values()), None)
            if sample_row:
                headers = [h for h in sample_row.keys() if h not in HIDDEN]
            else:
                headers = [
                    'date', 'amount', 'balance', 'description', 'reason',
                    'first_party_name', 'first_party_corporation', 'first_party_alliance',
                    'second_party_name', 'second_party_corporation', 'second_party_alliance',
                    'context', 'type',
                ]

            header_html = (
                "<tr>" +
                "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in headers) +
                "</tr>"
            )
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"
        except Exception as e:
            logger.error(f"Error generating transaction headers: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(f'Header generation error: {str(e)}')}\n\n"
            return

        cfg = BigBrotherConfig.get_solo()
        user_chars = get_user_characters(user_id)
        user_ids = set(user_chars.keys())
        safe_entities = get_safe_entities()
        batch_size = 100

        try:
            for i in range(0, total, batch_size):
                batch = qs[i : i + batch_size]
                rows_map = get_user_transactions(batch)
                sorted_keys = sorted(rows_map.keys(), key=lambda k: rows_map[k]['date'], reverse=True)

                for eid in sorted_keys:
                    row = rows_map[eid]
                    processed += 1
                    if processed % 10 == 0:
                        yield ": ping\n\n"

                    is_hostile = is_transaction_hostile(row, user_ids, safe_entities=safe_entities, entity_info_cache=row.get('info_cache'))
                    if is_hostile:
                        hostile_count += 1
                        cells = []
                        row_info_cache = row.get('info_cache')
                        for col in headers:
                            val = row.get(col, "")
                            text = html.escape(str(val))
                            style = ""
                            if col == 'type':
                                r_type = row.get('type', "")
                                if any(st in r_type for st in SUS_TYPES):
                                    style = 'color:red;'
                                if cfg.show_market_transactions and r_type in ["market_escrow", "market_transaction"]:
                                    style = 'color:red;'
                            elif col in ('first_party_name', 'second_party_name'):
                                pid = row.get(col.replace("_name", "_id"))
                                if pid and get_hostile_state(pid, 'character', when=row.get('date'), entity_info_cache=row_info_cache):
                                    style = 'color:red;'
                            elif col.endswith('corporation'):
                                cid = row.get(f"{col}_id")
                                if cid and get_hostile_state(cid, 'corporation', when=row.get('date'), entity_info_cache=row_info_cache):
                                    style = 'color:red;'
                            elif col.endswith('alliance'):
                                aid = row.get(f"{col}_id")
                                if aid and get_hostile_state(aid, 'alliance', when=row.get('date'), entity_info_cache=row_info_cache):
                                    style = 'color:red;'

                            style_attr = f' style="{style}"' if style else ""
                            cells.append(f"<td{style_attr}>{text}</td>")

                        tr_html = "<tr>" + "".join(cells) + "</tr>"
                        yield f"event: transaction\ndata:{json.dumps(tr_html)}\n\n"

                    if processed % 10 == 0 or processed == total:
                        yield (
                            "event: progress\n"
                            f"data:{processed},{total},{hostile_count}\n\n"
                        )

                connection.close()
                gc.collect()

            yield "event: done\ndata:bye\n\n"

        except Exception as e:
            logger.error(f"Error in transaction stream main loop: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(f'Stream error: {str(e)}')}\n\n"
            return

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@permission_required("aa_bb.basic_access")
def stream_assets_sse(request):
    """Stream hostile assets one system at a time via SSE."""
    option = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    def generator():
        try:
            yield ": ok\n\n"
            systems = get_asset_locations(user_id)
            total = len(systems)
            processed = hostile_count = 0

            if total == 0:
                yield "event: done\ndata:0\n\n"
                return

            safe_entities = get_safe_entities()
            now_ts = timezone.now()

            # Determine headers
            headers = ["System", "Location", "Character", "Owner", "Region"]
            header_html = "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            for system_id, data in systems.items():
                processed += 1
                system_name = data.get("name") or f"ID {system_id}"

                # Try to use cached owner info
                owner_info = get_entity_info(system_id, now_ts)
                system_owner_name = owner_info.get("name", "Unresolvable")
                region_name = owner_info.get("alli_name", "Unknown Region")

                rows_for_system = []
                for loc_id, loc_data in data.get("locations", {}).items():
                    loc_name = loc_data["name"]

                    # Use cached location owner info
                    loc_owner_info = get_entity_info(loc_id, now_ts)
                    oname = loc_owner_info.get("name", system_owner_name)

                    for asset in loc_data.get("assets", []):
                        char_id = asset["char_id"]

                        # Use cached character info for hostile check
                        is_hostile = get_hostile_state(
                            char_id,
                            'character',
                            system_id=system_id,
                            when=now_ts,
                            safe_entities=safe_entities,
                            entity_info_cache={
                                char_id: get_entity_info(char_id, now_ts),
                                system_id: owner_info,
                                loc_id: loc_owner_info
                            }
                        )

                        if is_hostile:
                            hostile_count += 1
                            owner_cell = f'<span class="text-danger">{html.escape(oname)}</span>'
                            tr = f"<tr><td>{html.escape(system_name)}</td><td>{html.escape(loc_name)}</td><td>{html.escape(asset['char_name'])}</td><td>{owner_cell}</td><td>{html.escape(region_name)}</td></tr>"
                            rows_for_system.append(tr)

                for tr in rows_for_system:
                    yield f"event: asset\ndata:{json.dumps(tr)}\n\n"

                yield f"event: progress\ndata:{processed},{total},{hostile_count}\n\n"

            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in asset stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@permission_required("aa_bb.basic_access")
def stream_clones_sse(request):
    """Stream hostile clones via SSE."""
    option = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    def generator():
        try:
            yield ": ok\n\n"
            systems = get_clones(user_id)
            total = len(systems)
            processed = hostile_count = 0

            if total == 0:
                yield "event: done\ndata:0\n\n"
                return

            safe_entities = get_safe_entities()
            now_ts = timezone.now()

            headers = ["System", "Station", "Character", "Clone Status", "Implants", "Owner", "Region"]
            header_html = "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            for system_id, data in systems.items():
                processed += 1
                system_name = data.get("name") or f"ID {system_id}"

                # Use cached owner info
                owner_info = get_entity_info(system_id, now_ts)
                system_owner_name = owner_info.get("name", "Unresolvable")
                region_name = owner_info.get("alli_name", "Unknown Region")

                for loc_id, loc_data in data.get("locations", {}).items():
                    loc_name = loc_data["name"]

                    # Use cached location owner info
                    loc_owner_info = get_entity_info(loc_id, now_ts)
                    oname = loc_owner_info.get("name", system_owner_name)

                    for clone in loc_data.get("clones", []):
                        char_id = clone["char_id"]

                        # Use cached character info for hostile check
                        is_hostile = get_hostile_state(
                            char_id,
                            'character',
                            system_id=system_id,
                            when=now_ts,
                            safe_entities=safe_entities,
                            entity_info_cache={
                                char_id: get_entity_info(char_id, now_ts),
                                system_id: owner_info,
                                loc_id: loc_owner_info
                            }
                        )

                        if is_hostile:
                            hostile_count += 1
                            owner_cell = f'<span class="text-danger">{html.escape(oname)}</span>'
                            implants_html = "<br>".join([html.escape(i) for i in clone["implants"]])
                            tr = f"<tr><td>{html.escape(system_name)}</td><td>{html.escape(loc_name)}</td><td>{html.escape(clone['char_name'])}</td><td>{html.escape(clone['jump_clone_name'])}</td><td>{implants_html}</td><td>{owner_cell}</td><td>{html.escape(region_name)}</td></tr>"
                            yield f"event: clone\ndata:{json.dumps(tr)}\n\n"

                yield f"event: progress\ndata:{processed},{total},{hostile_count}\n\n"

            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in clone stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


@login_required
@permission_required("aa_bb.basic_access")
def stream_contacts_sse(request):
    """Stream suspicious contacts via SSE."""
    option = request.GET.get("option", "")
    user_id = get_user_id(option)
    if not user_id:
        return HttpResponseBadRequest("Unknown account")

    if not corptools_active():
        return HttpResponseForbidden("Corptools required")

    def generator():
        try:
            yield ": ok\n\n"
            cfg = BigBrotherConfig.get_solo()
            exclude_neutral = cfg.exclude_neutral_contacts
            contacts = get_user_contacts(user_id)
            total = len(contacts)
            processed = hostile_count = 0

            if total == 0:
                yield "event: done\ndata:0\n\n"
                return

            headers = ["Character", "Contact Name", "Standing", "Contact Corp", "Contact Alliance"]
            header_html = "<tr>" + "".join(f"<th>{html.escape(h)}</th>" for h in headers) + "</tr>"
            yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

            safe_entities = get_safe_entities()
            now_ts = timezone.now()

            for cid, info in contacts.items():
                processed += 1
                standing = info.get('standing', 0)
                contact_id = info.get('contact_id') or cid
                contact_is_hostile = get_hostile_state(contact_id, info.get('contact_type'), when=now_ts, safe_entities=safe_entities)

                if standing < 0:
                    is_hostile = False
                else:
                    # skip neutral contacts if enabled
                    if exclude_neutral and standing == 0:
                        continue
                    # Positive or neutral standing is suspicious if the entity IS known as hostile
                    is_hostile = contact_is_hostile

                if is_hostile:
                    hostile_count += 1
                    my_chars = ", ".join(info.get('characters', []))
                    tr = f"<tr><td>{html.escape(my_chars)}</td><td>{html.escape(info['contact_name'])}</td><td>{info['standing']}</td><td>{html.escape(info['corporation'])}</td><td>{html.escape(info['alliance'])}</td></tr>"
                    yield f"event: contact\ndata:{json.dumps(tr)}\n\n"

                if processed % 10 == 0 or processed == total:
                    yield f"event: progress\ndata:{processed},{total},{hostile_count}\n\n"

            yield "event: done\ndata:bye\n\n"
        except Exception as e:
            logger.error(f"Error in contact stream for {option}: {e}", exc_info=True)
            yield f"event: error\ndata:{json.dumps(str(e))}\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp

def get_card_data(request, target_user_id: int, key: str):
    """Return card HTML and status tuple for the specified key."""

    if key == "compliance":  # Role/token compliance overview.
        content = render_user_roles_tokens_html(target_user_id)
        status = not (content and "danger" in content)

    elif key == "corp_bl":  # Inline corp blacklist check (with add links).
        issuer_id = request.user.id
        content   = get_add_to_blacklist_html(request, issuer_id, target_user_id) or "a"
        status    = not (content and "danger" in content)

    elif key == "alliance_bl":
        content = get_alliance_blacklist_link()
        status = True

    elif key == "external_bl":
        content = get_external_blacklist_link()
        status = True

    elif key == "freq_corp":  # Show frequent corporation changes timeline.
        content = get_frequent_corp_changes(target_user_id)
        status  = "danger" not in content

    elif key == "awox":  # Highlight kills where corp mates attacked each other.
        content = render_awox_kills_html(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "clone_states":  # Clone state availability (alpha/omega).
        content = render_character_states_html(target_user_id)
        status = not (content and "danger" in content)

    elif key == "sus_clones":  # Flag clones located in hostile space.
        content = render_clones(target_user_id)
        status  = not (content and any(w in content for w in ("danger", "warning")))

    elif key == "sus_asset":  # Summarize assets currently stranded in hostile systems.
        content = render_assets(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "sus_conta":  # Suspicious contact list card.
        content = render_contacts(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "sus_mail":  # Suspicious mail preview card.
        content = render_mails(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "sus_tra":  # Suspicious transaction summary card.
        content = render_transactions(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "cyno":  # Cyno readiness / history panel.
        content = render_user_cyno_info_html(target_user_id)
        status  = not (content and "danger" in content)

    elif key == "skills":  # Training gaps summary.
        content = render_user_skills_html(target_user_id)
        status  = not (content and "danger" in content)

    else:
        content = "WiP"
        status  = True

    return content, status


@require_POST
@permission_required("can_blacklist_characters")
def add_blacklist_view(request):
    """POST endpoint to add all of a target's characters to the corp blacklist."""
    issuer_id = int(request.POST["issuer_user_id"])
    target_id = int(request.POST["target_user_id"])
    reason    = request.POST.get("reason", "")
    added = add_user_characters_to_blacklist(
        issuer_user_id=issuer_id,
        target_user_id=target_id,
        reason=reason
    )
    messages.success(request, f"Blacklisted: {', '.join(added)}")
    return redirect(request.META.get("HTTP_REFERER", "/"))


@login_required
@permission_required("aa_bb.can_access_loa")
def loa_loa(request):
    """Display the LoA dashboard for the requesting user."""
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_loa_active:  # Feature toggle to hide LoA entirely.
        return render(request, "loa/disabled.html")
    user_requests = LeaveRequest.objects.filter(user=request.user).order_by('-created_at')
    return render(request, "loa/index.html", {"loa_requests": user_requests})

@login_required
@permission_required("aa_bb.can_view_all_loa")
def loa_admin(request):
    """Administrative LoA queue view with filtering."""
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_loa_active:  # Hide admin view when LoA disabled globally.
        return render(request, "loa/disabled.html")
    # Filtering
    qs = LeaveRequest.objects.select_related('user').order_by('-created_at')

    user_filter   = request.GET.get('user')
    status_filter = request.GET.get('status')

    if user_filter:  # Narrow to a single user's requests.
        qs = qs.filter(user__id=user_filter)
    if status_filter:  # Filter by request status (pending/approved/etc).
        qs = qs.filter(status=status_filter)

    # Build dropdown options from existing requests
    users_in_requests_qs = LeaveRequest.objects.all()

    users_in_requests = (
        users_in_requests_qs.values_list('user__id', 'user__username')
                            .distinct()
    )

    context = {
        'loa_requests': qs,
        'users': users_in_requests,
        'status_choices': LeaveRequest.STATUS_CHOICES,
        'current_user': user_filter,
        'current_status': status_filter,
    }
    return render(request, "loa/admin.html", context)

@login_required
@permission_required("aa_bb.can_access_loa")
def loa_request(request):
    """Handle LoA request creation form (GET/POST)."""
    cfg = BigBrotherConfig.get_solo()
    if not cfg.is_loa_active:  # Respect feature toggle.
        return render(request, "loa/disabled.html")

    if request.method == 'POST':  # Form submission branch.
        form = LeaveRequestForm(request.POST)
        if form.is_valid():  # Save request and ping staff.
            main_char = get_main_character_name(request.user.id)
            # 2) save with main_character
            lr = form.save(commit=False)
            lr.user = request.user
            lr.main_character = main_char
            lr.save()

            # 3) send webhook with character
            hook = cfg.loawebhook
            send_status_embed(
                subject="LoA Request",
                lines=[
                    f"{get_pings('LoA Request')} {main_char} requested LOA:",
                    f"- from **{lr.start_date}**",
                    f"- to **{lr.end_date}**",
                    f"- reason: **{lr.reason}**"
                ],
                color=0x3498db,
                hook=hook
            )

            return redirect('loa:index')
        else:
            form.add_error(None, "Please fill in all fields correctly.")
    else:
        form = LeaveRequestForm()

    return render(request, 'loa/request.html', {'form': form})

@login_required
@permission_required("aa_bb.can_access_loa")
def delete_request(request, pk):
    """Allow a user to delete their own pending LoA."""
    if request.method == 'POST':  # Only accept POST to mutate state.
        lr = get_object_or_404(LeaveRequest, pk=pk, user=request.user)
        if lr.user != request.user:  # Safety net in case of tampering.
            return HttpResponseForbidden("You may only delete your own requests.")
        elif lr.status == 'pending':  # Only pending requests may be removed.
            lr.delete()
            cfg = BigBrotherConfig.get_solo()
            hook = cfg.loawebhook
            send_status_embed(
                subject="LoA Deleted",
                lines=[
                    f"{get_pings('LoA Changed Status')} {lr.main_character} deleted their LOA:",
                    f"- from **{lr.start_date}**",
                    f"- to **{lr.end_date}**",
                    f"- reason: **{lr.reason}**"
                ],
                color=0x3498db,
                hook=hook
            )
    return redirect('loa:index')

@login_required
@permission_required("aa_bb.can_manage_loa")
def delete_request_admin(request, pk):
    """Admin-only delete path for any LoA request."""
    if request.method == 'POST':  # Guard mutation behind POST.
        cfg = BigBrotherConfig.get_solo()
        lr = get_object_or_404(LeaveRequest, pk=pk)
        lr.delete()
        hook = cfg.loawebhook
        userrr = get_main_character_name(request.user.id)
        send_status_embed(
            subject="LoA Deleted by Admin",
            lines=[
                f"{get_pings('LoA Changed Status')} {userrr} deleted {lr.main_character}'s LOA:",
                f"- from **{lr.start_date}**",
                f"- to **{lr.end_date}**",
                f"- reason: **{lr.reason}**"
            ],
            color=0x3498db,
            hook=hook
        )
    return redirect('loa:admin')

@login_required
@permission_required("aa_bb.can_manage_loa")
def approve_request(request, pk):
    """Mark an LoA approved and notify Discord."""
    if request.method == 'POST':  # Only process POST actions.
        cfg = BigBrotherConfig.get_solo()
        lr = get_object_or_404(LeaveRequest, pk=pk)
        lr.status = 'approved'
        lr.save()
        hook = cfg.loawebhook
        userrr = get_main_character_name(request.user.id)
        send_status_embed(
            subject="LoA Approved",
            lines=[
                f"{get_pings('LoA Changed Status')} {userrr} approved {lr.main_character}'s LOA:",
                f"- from **{lr.start_date}**",
                f"- to **{lr.end_date}**",
                f"- reason: **{lr.reason}**"
            ],
            color=0x3498db,
            hook=hook
        )
    return redirect('loa:admin')

@login_required
@permission_required("aa_bb.can_manage_loa")
def deny_request(request, pk):
    """Mark an LoA denied and notify Discord."""
    if request.method == 'POST':  # Only mutate via POST requests.
        cfg = BigBrotherConfig.get_solo()
        lr = get_object_or_404(LeaveRequest, pk=pk)
        lr.status = 'denied'
        lr.save()
        hook = cfg.loawebhook
        userrr = get_main_character_name(request.user.id)
        send_status_embed(
            subject="LoA Denied",
            lines=[
                f"{get_pings('LoA Changed Status')} {userrr} denied {lr.main_character}'s LOA:",
                f"- from **{lr.start_date}**",
                f"- to **{lr.end_date}**",
                f"- reason: **{lr.reason}**"
            ],
            color=0x3498db,
            hook=hook
        )
    return redirect('loa:admin')


@login_required
@permission_required("aa_bb.ticket_manager")
def ticket_list(request):
    """List compliance tickets."""
    from django.db.models import Count

    tab = request.GET.get('tab', 'open')

    if tab == 'all':
        tickets = ComplianceTicket.objects.all()
    elif tab == 'exceptions':
        tickets = ComplianceTicket.objects.filter(is_exception=True)
    elif tab == 'resolved':
        tickets = ComplianceTicket.objects.filter(is_resolved=True)
    else:  # 'open' is default
        tickets = ComplianceTicket.objects.filter(is_resolved=False, is_exception=False)

    if not afat_active():
        tickets = tickets.exclude(reason="paps_check")

    if not discordbot_active():
        tickets = tickets.exclude(reason="discord_check")

    tickets = tickets.select_related('user__profile__main_character').annotate(comment_count=Count('comments'))
    return render(request, 'aa_bb/ticket_list.html', {'tickets': tickets, 'current_tab': tab})


@login_required
@permission_required("aa_bb.ticket_manager")
def ticket_view(request, pk):
    """View details and comments for a specific ticket."""
    ticket = get_object_or_404(
        ComplianceTicket.objects.select_related('user__profile__main_character'),
        pk=pk
    )
    if ticket.reason == "paps_check" and not afat_active():
        return HttpResponseForbidden("PAP compliance tickets are hidden as afat is not active.")
    comments = ticket.comments.all().select_related('user__profile__main_character')
    return render(request, 'aa_bb/ticket_view.html', {'ticket': ticket, 'comments': comments})


@login_required
@permission_required("aa_bb.ticket_manager")
@require_POST
def ticket_resolve(request, pk):
    """Resolve a ticket via the UI."""
    ticket = get_object_or_404(ComplianceTicket, pk=pk)
    from .tasks_tickets import close_ticket, close_char_removed_ticket

    if ticket.reason in ["char_removed", "awox_kill"]:
        close_char_removed_ticket(ticket, user=request.user)
    else:
        close_ticket(ticket, user=request.user)

    return redirect('aa_bb:ticket_view', pk=pk)


@login_required
@permission_required("aa_bb.ticket_manager")
@require_POST
def ticket_reopen(request, pk):
    """Reopen a resolved ticket."""
    ticket = get_object_or_404(ComplianceTicket, pk=pk)
    from .tasks_tickets import reopen_ticket
    reopen_ticket(ticket, user=request.user)

    return redirect('aa_bb:ticket_view', pk=pk)


@login_required
@permission_required("aa_bb.ticket_manager")
@require_POST
def ticket_mark_exception(request, pk):
    """Mark a ticket as an exception."""
    ticket = get_object_or_404(ComplianceTicket, pk=pk)
    reason = request.POST.get('reason', '').strip()

    from .tasks_tickets import mark_ticket_exception
    mark_ticket_exception(ticket, request.user, reason)

    return redirect('aa_bb:ticket_view', pk=pk)


@login_required
@permission_required("aa_bb.ticket_manager")
@require_POST
def ticket_clear_exception(request, pk):
    """Clear exception status from a ticket."""
    ticket = get_object_or_404(ComplianceTicket, pk=pk)

    from .tasks_tickets import clear_ticket_exception
    clear_ticket_exception(ticket, request.user)

    return redirect('aa_bb:ticket_view', pk=pk)


@login_required
@permission_required("aa_bb.ticket_manager")
@require_POST
def ticket_add_comment(request, pk):
    """Add a comment to a ticket and optionally post to Discord."""
    ticket = get_object_or_404(ComplianceTicket, pk=pk)
    comment_text = request.POST.get('comment')
    if comment_text:
        from .tasks_tickets import add_ticket_comment
        add_ticket_comment(ticket, request.user, comment_text)

    return redirect('aa_bb:ticket_view', pk=pk)
