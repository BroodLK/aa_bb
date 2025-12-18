import html
import logging
import time
import json

from django.conf import settings
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
from django_celery_beat.models import PeriodicTask
from django.utils import timezone

from celery import shared_task
from celery.exceptions import Ignore

from allianceauth.authentication.models import UserProfile, CharacterOwnership

from .forms import LeaveRequestForm
from .app_settings import get_user_characters, get_entity_info, get_main_character_name, get_character_id, send_message, get_pings, aablacklist_active
from .models import BigBrotherConfig, WarmProgress, LeaveRequest

from aa_bb.checks.awox import render_awox_kills_html
from aa_bb.checks.corp_changes import get_frequent_corp_changes
from aa_bb.checks.cyno import render_user_cyno_info_html
from aa_bb.checks.hostile_assets import render_assets
from aa_bb.checks.hostile_clones import render_clones
# from aa_bb.checks.coalition_blacklist import generate_blacklist_links
# from aa_bb.checks.alliance_blacklist import get_user_character_names_alliance
from aa_bb.checks.sus_contacts import render_contacts
from aa_bb.checks.sus_mails import (
    is_mail_row_hostile,
    get_cell_style_for_mail_cell,
    gather_user_mails,
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
    from aa_bb.checks.corp_blacklist import (
        get_corp_blacklist_html,
        add_user_characters_to_blacklist,
        check_char_corp_bl,
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

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.DEBUG)

try:
    from corptools.models import Contract
except ImportError:
    logger.error("Corptools not not installed")


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
    """Return card configurations filtered by alliance permissions."""
    cards = CARD_DEFINITIONS
    try:
        cfg = BigBrotherConfig.get_solo()
    except BigBrotherConfig.DoesNotExist:
        return cards

    # if cfg.main_alliance_id != ALLOWED_ALLIANCE_ID:
    #     cards = [card for card in cards if card["key"] != "alliance_bl"]

    # if cfg.main_alliance_id not in ALLOWED_COALITION_ALLIANCE_IDS:
    #     cards = [card for card in cards if card["key"] != "coalition_bl"]

    if not aablacklist_active():
        cards = [card for card in cards if card["key"] != "corp_bl"]

    return cards


def get_user_id(character_name):
    """Resolve an auth user ID from a main character name."""
    try:
        ownership = CharacterOwnership.objects.select_related('user') \
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
    if key in ("sus_contr", "sus_mail","sus_tra"):  # Paginated cards handled separately via SSE/ajax.
        # handled via paginated endpoints
        return JsonResponse({"key": key, "title": title})

    target_user_id = get_user_id(option)
    if target_user_id is None:  # Unknown character selection.
        return JsonResponse({"error": "Unknown account"}, status=404)

    content, status = get_card_data(request, target_user_id, key)
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
        content, status = get_card_data(request, user_id, card["key"])
        cards.append({
            "title":   card["title"],
            "content": content,
            "status":  status,
        })
    return JsonResponse({"cards": cards})

@shared_task(bind=True)
def warm_entity_cache_task(self, user_id):
    """
    Gather mails, contracts, transactions; warm entity cache.
    Track progress in the DB via WarmProgress.
    """
    user_main = get_main_character_name(user_id) or str(user_id)
    qs = WarmProgress.objects.all()
    users = [
        {"user": wp.user_main, "current": wp.current, "total": wp.total}
        for wp in qs
    ]
    # Check for existing progress entry
    try:
        progress = WarmProgress.objects.get(user_main=user_main)
    except WarmProgress.DoesNotExist:
        progress = None

    if progress and progress.total > 0:  # Another warm job is in-flight for this pilot.
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
        if second_current != first_current:  # Existing job advanced, so bail out to avoid duplicate work.
            logger.info(
                f"[{user_main}] progress advanced from {first_current} to {second_current}; aborting new task."
            )
            raise Ignore(f"Task for {user_main} is already running.")
        else:
            logger.info(
                f"[{user_main}] no progress in 20 s (still {first_current}); continuing with new task."
            )

    # Build list of (entity_id, timestamp)
    entries = []
    contracts = gather_user_contracts(user_id)
    trans = gather_user_transactions(user_id)
    mails = gather_user_mails(user_id)
    candidates = []
    for c in contracts:
        issuer_id = get_character_id(c.issuer_name)
        candidates.append((issuer_id, getattr(c, "date_issued")))
        assignee = c.assignee_id or c.acceptor_id
        candidates.append((assignee, getattr(c, "date_issued")))
    for m in mails:
        candidates.append((m.from_id, getattr(m, "timestamp")))
        for mr in m.recipients.all():
            candidates.append((mr.recipient_id, getattr(m, "timestamp")))
    for t in trans:
        candidates.append((t.first_party_id, getattr(t, "date")))
        candidates.append((t.second_party_id, getattr(t, "date")))
    from django.db.models import Q
    from .models import EntityInfoCache
    query_filter = Q()
    for entity_id, as_of in candidates:
        query_filter |= Q(entity_id=entity_id, as_of=as_of)

    existing = set(
        EntityInfoCache.objects.filter(query_filter)
        .values_list('entity_id', 'as_of')
    )

    for candidate in candidates:
        if candidate not in existing:  # Only fetch entity info when cache lacks the tuple.
            entries.append(candidate)

    total = len(entries)
    logger.info(f"Starting warm cache for {user_main} ({total} entries)")

    # Initialize or update the progress record
    WarmProgress.objects.update_or_create(
        user_main=user_main,
        defaults={"current": 0, "total": total}
    )

    # Process each entry, updating the DB record
    for idx, (eid, ts) in enumerate(entries, start=1):
        WarmProgress.objects.filter(user_main=user_main).update(current=idx)
        get_entity_info(eid, ts)

    # Clean up when done
    WarmProgress.objects.filter(user_main=user_main).delete()
    logger.info(f"Completed warm cache for {user_main}")
    return total

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
    user_main = get_main_character_name(user_id) or str(user_id)
    WarmProgress.objects.get_or_create(
        user_main=user_main,
        defaults={"current": 0, "total": 0}
    )

    # Enqueue the celery task
    warm_entity_cache_task.delay(user_id)
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
    task_name = 'BB run regular updates'
    task = PeriodicTask.objects.filter(name=task_name).first()
    BigBrotherConfig.get_solo().is_active = True
    if not BigBrotherConfig.get_solo().is_active:  # Guard against misconfigured BB.
        msg = (
            "Big Brother is currently inactive; please fill settings and enable the task"
        )
        return render(request, "aa_bb/disabled.html", {"message": msg})

    if request.user.has_perm("aa_bb.full_access"):  # Full-access sees every main character.
        qs = UserProfile.objects.exclude(main_character=None)
    elif request.user.has_perm("aa_bb.recruiter_access"):  # Recruiters see only guest states.
        guest_states = BigBrotherConfig.get_solo().bb_guest_states.all()
        qs = UserProfile.objects.filter(state__in=guest_states).exclude(main_character=None)
    else:
        qs = None

    if qs is not None:  # Build dropdown choices only when the viewer has visibility.
        dropdown_options = (
            qs.values_list("main_character__character_name", flat=True)
              .order_by("main_character__character_name")
        )

    context = {
        "dropdown_options": dropdown_options,
        "CARD_DEFINITIONS": CARD_DEFINITIONS,
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
    qs = Contract.objects.filter(
        character__character__character_id__in=user_chars
    ).order_by('-date_issued').values_list('contract_id', 'date_issued')

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

    qs    = gather_user_contracts(user_id)
    total = qs.count()
    connection.close()

    def generator():
        # Initial SSE heartbeat
        yield ": ok\n\n"
        processed = hostile_count = 0

        if total == 0:  # Nothing to scan, emit done immediately.
            # Notify client that processing completed with zero hostile hits
            yield "event: done\ndata:0\n\n"
            return

        for c in qs:
            processed += 1
            # Ping to keep connection alive
            yield ": ping\n\n"

            issued = getattr(c, "date_issued", timezone.now())
            issuer_id = c.issuer_name.eve_id
            yield ": ping\n\n"
            cid = c.contract_id
            if c.assignee_id != 0:  # Contracts may target assignee or acceptor; prefer assignee when set.
                assignee_id = c.assignee_id
            else:
                assignee_id = c.acceptor_id
            yield ": ping\n\n"
            #logger.info(f"getting info for {issuer_id}")
            iinfo     = get_entity_info(issuer_id, issued)
            yield ": ping\n\n"
            #logger.info(f"getting info for {assignee_id}")
            ainfo     = get_entity_info(assignee_id, issued)
            yield ": ping\n\n"

            # Hydrate just this one

            row = {
                'contract_id':              cid,
                'issued_date':              issued,
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

            style_map = {
                col: get_cell_style_for_contract_row(col, row)
                for col in row
            }
            yield ": ping\n\n"
            row['cell_styles'] = style_map

            if is_contract_row_hostile(row):  # Emit rows that match hostile heuristics.
                hostile_count += 1
                tr_html = _render_contract_row_html(row)
                yield f"event: contract\ndata:{json.dumps(tr_html)}\n\n"

            # Progress update
            yield (
                "event: progress\n"
                f"data:{processed},{total},{hostile_count}\n\n"
            )
            connection.close()

        # Done
        yield "event: done\ndata:bye\n\n"

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
    "assignee_alliance", "status",
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
                        if check_char_corp_bl(rid):
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

    qs    = gather_user_mails(user_id)
    total = qs.count()
    connection.close()

    def generator():
        # initial SSE heartbeat
        yield ": ok\n\n"
        processed = hostile_count = 0

        if total == 0:  # Nothing to stream -> immediately finish.
            # Notify client that streaming finished without hostile mails
            yield "event: done\ndata:0\n\n"
            return

        for m in qs:
            processed += 1
            # per-mail ping
            yield ": ping\n\n"

            sent = getattr(m, "timestamp", timezone.now())

            # 1) hydrate sender
            sender_id = m.from_id
            #logger.info(f"getting info for {sender_id}")
            sinfo     = get_entity_info(sender_id, sent)
            yield ": ping\n\n"  # immediately after expensive call

            # 2) hydrate each recipient
            recipient_ids           = []
            recipient_names         = []
            recipient_corps         = []
            recipient_corp_ids      = []
            recipient_alliances     = []
            recipient_alliance_ids  = []
            for mr in m.recipients.all():
                rid   = mr.recipient_id
                #logger.info(f"getting info for {rid}")
                rinfo = get_entity_info(rid, sent)
                yield ": ping\n\n"  # after each recipient lookup

                recipient_ids.append(rid)
                recipient_names.append(rinfo["name"])
                recipient_corps.append(rinfo["corp_name"])
                recipient_corp_ids.append(rinfo["corp_id"])
                recipient_alliances.append(rinfo["alli_name"])
                recipient_alliance_ids.append(rinfo["alli_id"])

            # build the single-mail row dict
            row = {
                "message_id":              m.id_key,
                "sent_date":               sent,
                "subject":                 m.subject or "",
                "sender_name":             sinfo["name"],
                "sender_id":               sender_id,
                "sender_corporation":      sinfo["corp_name"],
                "sender_corporation_id":   sinfo["corp_id"],
                "sender_alliance":         sinfo["alli_name"],
                "sender_alliance_id":      sinfo["alli_id"],
                "recipient_names":         recipient_names,
                "recipient_ids":           recipient_ids,
                "recipient_corps":         recipient_corps,
                "recipient_corp_ids":      recipient_corp_ids,
                "recipient_alliances":     recipient_alliances,
                "recipient_alliance_ids":  recipient_alliance_ids,
                "status":                  "Read" if m.is_read else "Unread",
            }

            # 3) check hostility and, if hostile, stream the <tr>
            if is_mail_row_hostile(row):  # Emit only hostile mail rows.
                hostile_count += 1
                tr = _render_mail_row_html(row)
                yield f"event: mail\ndata:{json.dumps(tr)}\n\n"

            # 4) final per-mail progress
            yield (
                "event: progress\n"
                f"data:{processed},{total},{hostile_count}\n\n"
            )
            connection.close()

        # done
        yield "event: done\ndata:bye\n\n"

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

    qs    = gather_user_transactions(user_id)
    total = qs.count()
    connection.close()

    # Hidden columns for the transactions table
    HIDDEN        = {
        'first_party_id','second_party_id',
        'first_party_corporation_id','second_party_corporation_id',
        'first_party_alliance_id','second_party_alliance_id',
        'entry_id'
    }

    def generator():
        yield ": ok\n\n"                # initial heartbeat
        processed = hostile_count = 0

        if total == 0:  # No transactions -> stop immediately.
            # Notify client that processing ended without hostile entries
            yield "event: done\ndata:0\n\n"
            return

        # Determine headers from a single hydrated row (after empty check)
        sample_map = get_user_transactions(qs[:1])
        sample_row = next(iter(sample_map.values()), None)
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

        # Emit table header row once
        header_html = (
            "<tr>" +
            "".join(f"<th>{html.escape(h.replace('_',' ').title())}</th>" for h in headers) +
            "</tr>"
        )
        yield f"event: header\ndata:{json.dumps(header_html)}\n\n"

        for entry in qs:
            processed += 1
            yield ": ping\n\n"         # keep‐alive

            # hydrate this one entry
            row = get_user_transactions([entry])[entry.entry_id]

            if is_transaction_hostile(row):  # Only push rows that meet hostility rules.
                hostile_count += 1

                # build the <tr> using same style logic as render_transactions()
                cells = []
                cfg = BigBrotherConfig.get_solo()
                for col in headers:
                    val = row.get(col, "")
                    text = html.escape(str(val))
                    style = ""
                    # type‐based red
                    if col == 'type' and any(st in row['type'] for st in SUS_TYPES):
                        style = 'color:red;'
                    # first/second party name
                    if aablacklist_active():
                        if col in ('first_party_name','second_party_name'):
                            id_col = col.replace("_name", "_id")
                            pid = row[id_col]
                            if check_char_corp_bl(pid):
                                style = 'color:red;'
                    # corps & alliances
                    if col.endswith('corporation'):
                        cid = row[f"{col}_id"]
                        if cid and str(cid) in cfg.hostile_corporations:
                            style = 'color:red;'
                    if col.endswith('alliance'):
                        aid = row[f"{col}_id"]
                        if aid and str(aid) in cfg.hostile_alliances:
                            style = 'color:red;'
                    def make_td(text, style=""):
                        style_attr = f' style="{style}"' if style else ""
                        return f"<td{style_attr}>{text}</td>"
                    cells.append(make_td(text, style))
                tr_html = "<tr>" + "".join(cells) + "</tr>"
                yield f"event: transaction\ndata:{json.dumps(tr_html)}\n\n"

            # progress update
            yield (
                "event: progress\n"
                f"data:{processed},{total},{hostile_count}\n\n"
            )
            connection.close()

        # Done
        yield "event: done\ndata:bye\n\n"

    resp = StreamingHttpResponse(generator(), content_type='text/event-stream')
    resp["Cache-Control"]     = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


# Card data helper

def get_card_data(request, target_user_id: int, key: str):
    """Return card HTML and status tuple for the specified key."""

    if key == "compliance":  # Role/token compliance overview.
        content = render_user_roles_tokens_html(target_user_id)
        status = not (content and "danger" in content)

    elif key == "corp_bl":  # Inline corp blacklist check (with add links).
        issuer_id = request.user.id
        content   = get_corp_blacklist_html(request, issuer_id, target_user_id) or "a"
        status    = not (content and "danger" in content)

    # elif key == "alliance_bl":
    #     from django.contrib.auth.models import User
    #     try:
    #         from blacklist.models import BlacklistFilter
    #         try:
    #             url = getattr(settings, "SITE_URL", "").rstrip("/")
    #
    #             # Use the Smart Filter to see if this user hits the blacklist
    #             bf = BlacklistFilter()
    #             results = bf.audit_filter(User.objects.filter(pk=target_user_id))
    #             data = results.get(target_user_id, {"message": "", "check": True})
    #             if not data:
    #                 content = "No Data"
    #                 status = True
    #                 return content, status
    #
    #             is_clean = data.get("check", True)
    #
    #             names = data.get("message", "")
    #
    #             if is_clean:
    #                 # No blacklisted hits for this user
    #                 content = (
    #                     "No blacklisted characters found for this user."
    #                 )
    #                 status = True
    #             else:
    #                 # User has blacklist hits; show them and link to the blacklist app
    #                 names_html = names.replace(",", "<br>")
    #                 content = (
    #                     "The following characters (or their corps/alliances) are on the "
    #                     "blacklist:<br><br>"
    #                     f"{names_html}<br><br>"
    #                     f"Go <a href='{url}/blacklist/blacklist/'>here</a> for full details."
    #                 )
    #                 status = False
    #         except Exception as e:
    #             content = (e)
    #             status = False
    #     except ImportError:
    #         content = ('blacklist is not installed')
    #         status  = True

    elif key == "freq_corp":  # Show frequent corporation changes timeline.
        content = get_frequent_corp_changes(target_user_id)
        status  = "danger" not in content

    elif key == "awox":  # Highlight kills where corp mates attacked each other.
        content = render_awox_kills_html(target_user_id)
        status  = content is None

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
        status  = not content

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
    return redirect(
        request.META.get("HTTP_REFERER", "/"),
        message=f"Blacklisted: {', '.join(added)}"
    )


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
    users_in_requests = (
        LeaveRequest.objects
                    .values_list('user__id', 'user__username')
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
            send_message(f"##{get_pings('LoA Request')} {main_char} requested LOA:\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- reason: **{lr.reason}**", hook)

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
            hook = BigBrotherConfig.get_solo().loawebhook
            send_message(f"##{get_pings('LoA Changed Status')} {lr.main_character} deleted their LOA:\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- reason: **{lr.reason}**", hook)
    return redirect('loa:index')

@login_required
@permission_required("aa_bb.can_manage_loa")
def delete_request_admin(request, pk):
    """Admin-only delete path for any LoA request."""
    if request.method == 'POST':  # Guard mutation behind POST.
        lr = get_object_or_404(LeaveRequest, pk=pk, user=request.user)
        lr.delete()
        hook = BigBrotherConfig.get_solo().loawebhook
        userrr = get_main_character_name(request.user.id)
        send_message(f"##{get_pings('LoA Changed Status')} {userrr} deleted {lr.main_character}'s LOA:\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- reason: **{lr.reason}**", hook)
    return redirect('loa:admin')

@login_required
@permission_required("aa_bb.can_manage_loa")
def approve_request(request, pk):
    """Mark an LoA approved and notify Discord."""
    if request.method == 'POST':  # Only process POST actions.
        lr = get_object_or_404(LeaveRequest, pk=pk)
        lr.status = 'approved'
        lr.save()
        hook = BigBrotherConfig.get_solo().loawebhook
        userrr = get_main_character_name(request.user.id)
        send_message(f"##{get_pings('LoA Changed Status')} {userrr} approved {lr.main_character}'s LOA:\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- reason: **{lr.reason}**", hook)
    return redirect('loa:admin')

@login_required
@permission_required("aa_bb.can_manage_loa")
def deny_request(request, pk):
    """Mark an LoA denied and notify Discord."""
    if request.method == 'POST':  # Only mutate via POST requests.
        lr = get_object_or_404(LeaveRequest, pk=pk)
        lr.status = 'denied'
        lr.save()
        hook = BigBrotherConfig.get_solo().loawebhook
        userrr = get_main_character_name(request.user.id)
        send_message(f"##{get_pings('LoA Changed Status')} {userrr} denied {lr.main_character}'s LOA:\n- from **{lr.start_date}**\n- to **{lr.end_date}**\n- reason: **{lr.reason}**", hook)
    return redirect('loa:admin')
