"""
Counterintelligence views and dashboards.
"""

# Standard Library
from collections import Counter
from datetime import timedelta

# Django
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import (
    Count,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
)
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

# AA BigBrother
from aa_bb.models import BigBrotherConfig

from .ingest.paps import FCRosterIngestor
from .ingest.zkill import ZKillIngestor
from .models import (
    CICorrelation,
    CIEvent,
    CIIdentity,
    CIInvestigation,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CIOverride,
    CISession,
)
from .scoring import RISK_MATRIX_LOOKBACK_DAYS, scoring
from .services.agent import CIAgentService
from .services.baseline import BaselineEngine
from .services.bb_signals import BBSignals
from .services.correlation import CorrelationEngine
from .services.operations import CIOperationService
from .tasks import (
    ZKILL_SCAN_DELAY_MINUTES,
    recompute_leak_exposure_task,
    rescan_operation_combat_task,
)


def _fc_candidates():
    """Active members who can be selected as an operation's fleet commander."""
    member_states = BigBrotherConfig.get_solo().bb_member_states.all()
    return (
        User.objects.filter(is_active=True, profile__state__in=member_states)
        .select_related("profile__main_character")
        .order_by("username")[:200]
    )


def _count_per_user(model):
    """Per-user row count as a subquery, so several counts don't multiply rows."""
    counts = (
        model.objects.filter(user=OuterRef("pk"))
        .order_by()
        .values("user")
        .annotate(c=Count("id"))
        .values("c")
    )
    return Coalesce(Subquery(counts, output_field=IntegerField()), 0)


def _querystring(request, drop=("page",)) -> str:
    """Current GET parameters without ``drop``, for links that keep filters."""
    params = request.GET.copy()
    for key in drop:
        params.pop(key, None)
    return params.urlencode()


def _sort_param(request, allowed: dict, default: str, default_dir: str = "asc"):
    """
    Whitelisted ``?sort=<key>&dir=asc|desc``. Returns (key, descending, value)
    where value is ``allowed[key]``; unknown keys fall back to the default.
    """
    key = request.GET.get("sort", default)
    if key not in allowed:
        key = default
    direction = request.GET.get("dir", default_dir)
    if direction not in ("asc", "desc"):
        direction = default_dir
    return key, direction == "desc", allowed[key]


def _sort_context(request, sort_key: str, descending: bool) -> dict:
    """Template context consumed by _sort_header.html."""
    return {
        "sort": sort_key,
        "dir": "desc" if descending else "asc",
        # Header links replace sort/dir and reset to the first page
        "sort_querystring": _querystring(request, drop=("page", "sort", "dir")),
        "querystring": _querystring(request),
    }


def _secrecy_levels() -> list:
    """(value, label) choices for the operation secrecy select."""
    return list(CIOperation.SECRECY_LEVELS.items())


@permission_required("aa_bb.can_view_ci")
def operation_list(request):
    """
    List all operations with participation and objective summaries.
    """
    operations = CIOperation.objects.prefetch_related(
        "participants", "objectives", "correlations"
    ).order_by("-start_time")

    paginator = Paginator(operations, 25)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "title": _("Counterintelligence - Operations"),
    }
    if request.user.has_perm("aa_bb.can_manage_ci"):
        context.update(fc_candidates=_fc_candidates(), secrecy_levels=_secrecy_levels())
    return render(request, "aa_bb/counterintel/operation_list.html", context)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def create_operation_view(request):
    """
    Create an operation by hand, for fleets, briefings or strategic activity not
    tracked in AFAT.
    """
    name = request.POST.get("name", "").strip()
    start_time = _parse_input_datetime(request.POST.get("start_time"))
    end_time = _parse_input_datetime(request.POST.get("end_time"))
    secrecy_level = request.POST.get("secrecy_level", "standard").strip()

    if not name or not start_time:
        messages.error(request, _("An operation needs a name and a start time."))
        return redirect("ci:operation_list")
    if end_time and end_time < start_time:
        messages.error(
            request, _("The end time cannot be earlier than the start time.")
        )
        return redirect("ci:operation_list")
    if secrecy_level not in CIOperation.SECRECY_LEVELS:
        secrecy_level = "standard"

    fleet_commander = None
    fc_id = request.POST.get("fleet_commander_id")
    if fc_id and fc_id.isdigit():
        fleet_commander = User.objects.filter(id=int(fc_id)).first()

    operation = CIOperationService.create_operation(
        name=name,
        start_time=start_time,
        end_time=end_time,
        operation_type=request.POST.get("operation_type", "").strip() or "strategic",
        fleet_commander=fleet_commander,
        secrecy_level=secrecy_level,
        notes=request.POST.get("notes", "").strip(),
    )
    if end_time:
        # The manager set the window; fleet syncs must not overwrite it
        operation.window_locked = True
        operation.save(update_fields=["window_locked"])

    messages.success(request, _(f"Operation '{operation.name}' created."))
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_view_ci")
def ci_help(request):
    """
    How Counterintelligence works and how to run it. Thresholds and weights are
    read from the live scoring settings so the page cannot drift from behaviour.
    """
    # AA BigBrother
    from aa_bb.management.commands.sync_counterintel_tasks import TASKS

    from .tasks import AFAT_SYNC_DEBOUNCE_SECONDS

    risk_factor_labels = {
        "comms": _(
            "Comms & in-game presence: comms without fleet credit, comms or "
            "in-game logoff before contact"
        ),
        "discord": _("Discord text inactivity"),
        "alt": _(
            "Alts: alt near the fight, alt in a hostile entity, shared Mumble "
            "name, uncredited alt online"
        ),
        "deviation": _("Logoff against the member's own habit"),
        "bb_signals": _("Big Brother hostile-tie flags (any operation)"),
        "leak": _("Repeated exposure to leaked operations"),
        "accountability": _("Flying characters without a corptools audit"),
    }
    cfg = scoring()
    risk_weights = [
        (risk_factor_labels.get(key, key), round(weight * 100))
        for key, weight in cfg.risk_weights().items()
    ]
    engine = CorrelationEngine
    return render(
        request,
        "aa_bb/counterintel/help.html",
        {
            "cfg": cfg,
            "alert_confidence": cfg.alert_confidence,
            "alert_risk": cfg.alert_risk,
            "no_channel_cap": cfg.comms_no_channel_cap,
            "insufficient_baseline_confidence": cfg.conf_pre_contact_no_history,
            "idle_presence_hours": engine.IDLE_PRESENCE_HOURS,
            "briefing_days": engine.BRIEFING_DEPLOYMENT_DAYS,
            "leak_lookback_days": engine.LEAK_LOOKBACK_DAYS,
            "leak_min_ops": cfg.leak_min_ops,
            "leak_min_rate_pct": round(cfg.leak_min_rate * 100),
            "leak_min_lift": cfg.leak_min_lift,
            "auto_triage_confidence": cfg.auto_triage_confidence,
            "auto_override_days": cfg.override_days,
            "never_auto_exempt": CIOverride.NEVER_AUTO_EXEMPT,
            "risk_weights": risk_weights,
            "secrecy_levels": [
                (label, cfg.secrecy_multiplier(value))
                for value, label in CIOperation.SECRECY_LEVELS.items()
            ],
            "scoring_admin_url": (
                reverse("admin:aa_bb_ciscoringconfig_change")
                if request.user.is_superuser
                else None
            ),
            "open_window_min_hours": int(
                CIOperation.OPEN_WINDOW_MIN.total_seconds() // 3600
            ),
            "open_window_max_hours": int(
                CIOperation.OPEN_WINDOW_MAX.total_seconds() // 3600
            ),
            "bb_flags": BBSignals.flag_severities(),
            "pre_window_minutes": CIOperationService.DEFAULT_PRE_WINDOW_MINUTES,
            "zkill_delay_minutes": ZKILL_SCAN_DELAY_MINUTES,
            "afat_debounce_seconds": AFAT_SYNC_DEBOUNCE_SECONDS,
            "min_baseline_disconnects": BaselineEngine.MIN_BASELINE_DISCONNECTS,
            "scheduled_tasks": TASKS,
            "title": _("Counterintelligence - Help"),
        },
    )


@permission_required("aa_bb.can_view_ci")
def operation_timeline(request, operation_id: int):
    """
    Detailed operational timeline dashboard reconciling comms, roster, and combat.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    timeline = CIOperationService.redact_for_display(
        CIOperationService.reconstruct_timeline(operation)
    )

    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.GET.get("format") == "json"
    ):
        return JsonResponse(timeline, safe=False)

    return render(
        request,
        "aa_bb/counterintel/operation_timeline.html",
        {
            "operation": operation,
            "timeline": timeline,
            "fc_candidates": _fc_candidates(),
            "secrecy_levels": _secrecy_levels(),
            "title": f"{operation.name} - {_('Operation Timeline')}",
        },
    )


@permission_required("aa_bb.can_view_ci")
def member_list(request):
    """
    Directory of tracked members with communication sessions and correlation findings.
    """
    cfg = BigBrotherConfig.get_solo()
    member_states = cfg.bb_member_states.all()
    users = (
        User.objects.filter(is_active=True, profile__state__in=member_states)
        .select_related("profile__main_character", "userstatus")
        .annotate(
            session_count=_count_per_user(CISession),
            fleet_count=_count_per_user(CIOperationParticipant),
            finding_count=_count_per_user(CICorrelation),
        )
        .distinct()
    )
    search_q = request.GET.get("q", "").strip()
    if search_q:
        users = users.filter(
            Q(username__icontains=search_q)
            | Q(profile__main_character__character_name__icontains=search_q)
        )

    member_filter = request.GET.get("filter", "")
    member_filters = {
        "with_findings": Q(finding_count__gt=0),
        "no_fleets": Q(fleet_count=0),
        "no_comms": Q(session_count=0),
        "never_discord": Q(userstatus__last_discord_message_at__isnull=True),
    }
    if member_filter in member_filters:
        users = users.filter(member_filters[member_filter])

    sort_key, descending, order_field = _sort_param(
        request,
        {
            "member": "username",
            "main": "profile__main_character__character_name",
            "sessions": "session_count",
            "fleets": "fleet_count",
            "discord": "userstatus__last_discord_message_at",
            "findings": "finding_count",
        },
        default="member",
    )
    order = (
        F(order_field).desc(nulls_last=True)
        if descending
        else F(order_field).asc(nulls_last=True)
    )
    users = users.order_by(order, "username")

    paginator = Paginator(users, 30)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "aa_bb/counterintel/member_list.html",
        {
            "page_obj": page_obj,
            "search_q": search_q,
            "member_filter": member_filter,
            "member_filter_choices": [
                ("with_findings", _("With findings")),
                ("no_fleets", _("No fleets")),
                ("no_comms", _("No comms")),
                ("never_discord", _("Never on Discord")),
            ],
            **_sort_context(request, sort_key, descending),
            "title": _("Counterintelligence - Member Directory"),
        },
    )


@permission_required("aa_bb.can_view_ci")
def member_activity(request, user_id: int):
    """
    Longitudinal communication and fleet activity view for a specific member.
    """
    cfg = BigBrotherConfig.get_solo()
    member_states = cfg.bb_member_states.all()
    subject_user = get_object_or_404(
        User.objects.filter(profile__state__in=member_states), id=user_id
    )
    identities = CIIdentity.objects.filter(user=subject_user).order_by(
        "-effective_from"
    )
    sessions = CISession.objects.filter(user=subject_user).order_by("-start_time")[:50]
    eve_activity = CIEvent.objects.filter(
        user=subject_user, source="eve_activity"
    ).order_by("-timestamp")[:50]
    mumble_events = CIEvent.objects.filter(user=subject_user, source="mumble").order_by(
        "-timestamp"
    )[:50]
    participations = (
        CIOperationParticipant.objects.filter(user=subject_user)
        .select_related("operation")
        .order_by("-recorded_at")[:50]
    )
    correlations = (
        CICorrelation.objects.filter(user=subject_user)
        .select_related("operation")
        .order_by("-computed_at")
    )
    overrides = CIOverride.objects.filter(user=subject_user).order_by("-created_at")

    # AA BigBrother
    from aa_bb.models import UserStatus

    user_status = UserStatus.objects.filter(user=subject_user).first()
    last_discord_chat = UserStatus.get_last_discord_message_at_for_user(subject_user)

    return render(
        request,
        "aa_bb/counterintel/member_activity.html",
        {
            "subject_user": subject_user,
            "identities": identities,
            "sessions": sessions,
            "eve_activity": eve_activity,
            "mumble_events": mumble_events,
            "participations": participations,
            "correlations": correlations,
            "overrides": overrides,
            "user_status": user_status,
            "last_discord_chat": last_discord_chat,
            "bb_signals": BBSignals.for_user(subject_user.id),
            "hide_discord_channels": scoring().hide_discord_channel_names,
            "title": f"{subject_user.username} - {_('Member Activity History')}",
        },
    )


@permission_required("aa_bb.can_review_ci")
def review_queue(request):
    """
    Review queue for triage of flagged anomalies and investigation cases.
    """
    status_filter = request.GET.get("status", "pending")
    investigations = CIInvestigation.objects.select_related(
        "subject_user__profile__main_character", "operation", "assigned_reviewer"
    )

    if status_filter and status_filter != "all":
        investigations = investigations.filter(status=status_filter)

    search_q = request.GET.get("q", "").strip()
    if search_q:
        text_match = (
            Q(subject_user__username__icontains=search_q)
            | Q(
                subject_user__profile__main_character__character_name__icontains=search_q
            )
            | Q(operation__name__icontains=search_q)
        )
        case_number = search_q.lstrip("#")
        if case_number.isdigit():
            text_match |= Q(id=int(case_number))
        investigations = investigations.filter(text_match)

    reviewer_filter = request.GET.get("reviewer", "")
    if reviewer_filter == "unassigned":
        investigations = investigations.filter(assigned_reviewer__isnull=True)
    elif reviewer_filter == "mine":
        investigations = investigations.filter(assigned_reviewer=request.user)

    anomaly_filter = request.GET.get("anomaly", "")
    anomaly_choices = sorted(
        CICorrelation.objects.order_by()
        .values_list("anomaly_type", flat=True)
        .distinct()
    )
    if anomaly_filter in anomaly_choices:
        investigations = investigations.filter(
            Exists(
                CICorrelation.objects.filter(
                    user=OuterRef("subject_user"),
                    operation=OuterRef("operation"),
                    anomaly_type=anomaly_filter,
                )
            )
        )

    sort_key, descending, order_fields = _sort_param(
        request,
        {
            "created": ("created_at",),
            "case": ("id",),
            "subject": ("subject_user__username", "-created_at"),
            "operation": ("operation__start_time", "-created_at"),
            "status": ("status", "-created_at"),
            "reviewer": ("assigned_reviewer__username", "-created_at"),
        },
        default="created",
        default_dir="desc",
    )
    primary, *tiebreak = order_fields
    primary_order = (
        F(primary).desc(nulls_last=True)
        if descending
        else F(primary).asc(nulls_last=True)
    )
    investigations = investigations.order_by(primary_order, *tiebreak)

    paginator = Paginator(investigations, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    # Attach structured benign explanations for immediate context
    for case in page_obj:
        case.benign_explanations = CIAgentService.get_benign_explanations(case)

    return render(
        request,
        "aa_bb/counterintel/review_queue.html",
        {
            "page_obj": page_obj,
            "current_status": status_filter,
            "status_choices": CIInvestigation.STATUS_CHOICES,
            "search_q": search_q,
            "reviewer_filter": reviewer_filter,
            "anomaly_filter": anomaly_filter,
            "anomaly_choices": anomaly_choices,
            "status_querystring": _querystring(request, drop=("page", "status")),
            "status_buttons": [
                ("all", _("All")),
                ("pending", _("Needs Review")),
                ("in_review", _("In Review")),
                ("escalated", _("Escalated")),
                ("cleared", _("Cleared")),
                ("dismissed", _("Dismissed")),
            ],
            **_sort_context(request, sort_key, descending),
            "title": _("Counterintelligence - Investigation Review Queue"),
        },
    )


@permission_required("aa_bb.can_review_ci")
def investigation_detail(request, investigation_id: int):
    """
    Detail and disposition view for an investigation case.
    """
    investigation = get_object_or_404(
        CIInvestigation.objects.select_related(
            "subject_user", "operation", "assigned_reviewer"
        ),
        id=investigation_id,
    )

    if request.method == "POST":
        new_status = request.POST.get("status")
        reviewer_notes = request.POST.get("reviewer_notes", "").strip()
        resolution_tag = request.POST.get("resolution_tag", "").strip()

        if new_status in dict(CIInvestigation.STATUS_CHOICES):
            investigation.status = new_status
        if resolution_tag in dict(CIInvestigation.RESOLUTION_CHOICES):
            investigation.resolution_tag = resolution_tag
        if reviewer_notes:
            investigation.reviewer_notes = reviewer_notes
        if not investigation.assigned_reviewer:
            investigation.assigned_reviewer = request.user

        # False-Positive Feedback Loop: time-limited CIOverride if cleared/dismissed with tag
        if (
            investigation.status in ("cleared", "dismissed")
            and investigation.resolution_tag
        ):
            CIOverride.create_from_disposition(
                investigation,
                created_by=request.user,
                reason=f"Auto-generated from Investigation #{investigation.id} ({investigation.get_resolution_tag_display()})",
            )

        investigation.save()
        investigation.update_dossier_hash()
        messages.success(request, _("Investigation updated successfully."))
        return redirect("ci:investigation_detail", investigation_id=investigation.id)

    # Correlated anomalies for this user & op
    correlations = []
    timeline = None
    if investigation.operation:
        correlations = CICorrelation.objects.filter(
            operation=investigation.operation,
            user=investigation.subject_user,
        )
        timeline = CIOperationService.redact_for_display(
            CIOperationService.reconstruct_timeline(investigation.operation)
        )

    benign_explanations = CIAgentService.get_benign_explanations(investigation)

    return render(
        request,
        "aa_bb/counterintel/investigation_detail.html",
        {
            "investigation": investigation,
            "correlations": correlations,
            "timeline": timeline,
            "benign_explanations": benign_explanations,
            "status_choices": CIInvestigation.STATUS_CHOICES,
            "resolution_choices": CIInvestigation.RESOLUTION_CHOICES,
            "auto_override_days": scoring().override_days,
            "title": f"Investigation - {investigation.subject_user.username}",
        },
    )


@permission_required("aa_bb.can_review_ci")
def export_investigation_dossier(request, investigation_id: int):
    """
    Exports a cryptographically-signed canonical JSON dossier of the investigation for chain of custody.
    """
    investigation = get_object_or_404(CIInvestigation, id=investigation_id)
    investigation.update_dossier_hash()
    payload = investigation.compute_dossier_payload()
    payload["dossier_hash"] = investigation.dossier_hash

    response = JsonResponse(payload, json_dumps_params={"indent": 2, "sort_keys": True})
    filename = f"ci_investigation_{investigation.id}_{investigation.subject_user.username}_{timezone.now():%Y%m%d}.json"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@permission_required("aa_bb.can_view_ci")
def member_dossier(request, user_id: int):
    """
    Generates and returns an aggregated intelligence dossier for a member.
    """
    user = get_object_or_404(User, id=user_id)
    dossier = CIAgentService.generate_case_dossier(user)

    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.GET.get("format") == "json"
    ):
        return JsonResponse(dossier)

    return render(
        request,
        "aa_bb/counterintel/member_activity.html",
        {
            "subject_user": user,
            "dossier": dossier,
            "title": f"Dossier - {user.username}",
        },
    )


@permission_required("aa_bb.can_view_ci")
def risk_matrix(request):
    """
    Alliance-wide Member Risk Matrix Dashboard. A member's risk index is the highest
    risk score among their recent findings, ignoring anomaly types exempted by a
    CIOverride and findings whose investigation was cleared or dismissed.
    """
    cfg = BigBrotherConfig.get_solo()
    member_states = cfg.bb_member_states.all()
    lookback = timezone.now() - timedelta(days=RISK_MATRIX_LOOKBACK_DAYS)

    recent_correlations = CICorrelation.objects.filter(
        operation__start_time__gte=lookback
    ).select_related("operation")
    users_qs = (
        User.objects.filter(is_active=True, profile__state__in=member_states)
        .select_related("profile__main_character", "profile__state", "userstatus")
        .prefetch_related(
            Prefetch(
                "ci_correlations",
                queryset=recent_correlations,
                to_attr="recent_correlations",
            ),
            Prefetch(
                "ci_overrides",
                queryset=CIOverride.objects.active(),
                to_attr="override_list",
            ),
        )
        .distinct()
    )

    search_q = request.GET.get("q", "").strip()
    tier_filter = request.GET.get("tier", "").strip().upper()
    state_filter = request.GET.get("state", "").strip()
    open_cases_only = request.GET.get("open_cases") == "1"

    if search_q:
        users_qs = users_qs.filter(
            Q(username__icontains=search_q)
            | Q(profile__main_character__character_name__icontains=search_q)
        )
    if state_filter.isdigit():
        users_qs = users_qs.filter(profile__state_id=int(state_filter))

    resolved_cases = set(
        CIInvestigation.objects.filter(status__in=("cleared", "dismissed")).values_list(
            "subject_user_id", "operation_id"
        )
    )
    open_case_counts = Counter(
        CIInvestigation.objects.filter(status__in=("pending", "in_review")).values_list(
            "subject_user_id", flat=True
        )
    )

    all_members_data = []
    high_risk_count = 0
    med_risk_count = 0
    low_risk_count = 0

    for u in users_qs:
        exempt_types = {
            anomaly_type
            for ovr in u.override_list
            for anomaly_type in (ovr.exempt_anomaly_types or [])
        }
        corrs = [
            c
            for c in u.recent_correlations
            if c.anomaly_type not in exempt_types
            and (u.id, c.operation_id) not in resolved_cases
        ]
        effective_risk = max((c.risk_score for c in corrs), default=0.0)

        if effective_risk >= 0.70:
            tier = "HIGH"
            tier_class = "danger"
            high_risk_count += 1
        elif effective_risk >= 0.40:
            tier = "MEDIUM"
            tier_class = "warning"
            med_risk_count += 1
        else:
            tier = "LOW"
            tier_class = "success"
            low_risk_count += 1

        if tier_filter and tier != tier_filter:
            continue
        if open_cases_only and not open_case_counts.get(u.id):
            continue

        profile = getattr(u, "profile", None)
        user_status = getattr(u, "userstatus", None)

        all_members_data.append(
            {
                "user": u,
                "main_character": getattr(profile, "main_character", None),
                "state": getattr(profile, "state", None),
                "risk_score": round(effective_risk, 2),
                "risk_tier": tier,
                "risk_class": tier_class,
                "anomaly_count": len(corrs),
                "top_anomalies": [
                    anomaly_type.replace("_", " ")
                    for anomaly_type, _count in Counter(
                        c.anomaly_type for c in corrs
                    ).most_common(3)
                ],
                "last_finding_at": max(
                    (c.operation.start_time for c in corrs), default=None
                ),
                "open_cases": open_case_counts.get(u.id, 0),
                "last_discord": getattr(user_status, "last_discord_message_at", None),
            }
        )

    sort_key, descending, sort_value = _sort_param(
        request,
        {
            "risk": lambda x: (x["risk_score"], x["anomaly_count"]),
            "member": lambda x: x["user"].username.lower(),
            "state": lambda x: str(x["state"] or "").lower(),
            "findings": lambda x: x["anomaly_count"],
            "last_finding": lambda x: x["last_finding_at"],
            "open_cases": lambda x: x["open_cases"],
            "discord": lambda x: x["last_discord"],
        },
        default="risk",
        default_dir="desc",
    )
    # Rows without a value (no finding, never on Discord) go last either way
    present = [x for x in all_members_data if sort_value(x) is not None]
    missing = [x for x in all_members_data if sort_value(x) is None]
    present.sort(key=sort_value, reverse=descending)
    all_members_data = present + missing

    paginator = Paginator(all_members_data, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "aa_bb/counterintel/risk_matrix.html",
        {
            "page_obj": page_obj,
            "search_q": search_q,
            "tier_filter": tier_filter,
            "state_filter": state_filter,
            "open_cases_only": open_cases_only,
            "member_states": member_states,
            "tier_querystring": _querystring(request, drop=("page", "tier")),
            **_sort_context(request, sort_key, descending),
            "high_risk_count": high_risk_count,
            "med_risk_count": med_risk_count,
            "low_risk_count": low_risk_count,
            "total_members": high_risk_count + med_risk_count + low_risk_count,
            "lookback_days": RISK_MATRIX_LOOKBACK_DAYS,
            "title": _("Counterintelligence - Member Risk Matrix"),
        },
    )


@permission_required("aa_bb.can_review_ci")
@require_POST
def quick_disposition(request, investigation_id: int):
    """
    One-click case disposition handler for rapid, low-friction triage
    of benign or confirmed cases.
    """
    investigation = get_object_or_404(CIInvestigation, id=investigation_id)
    action = request.POST.get("action", "").strip()

    action_map = {
        "clear_scout": (
            "cleared",
            "scout_alt",
            _("[One-Click] Cleared: Verified Alt / Scout / Support operational role."),
        ),
        "clear_habitual": (
            "cleared",
            "benign_coincidence",
            _(
                "[One-Click] Cleared: Routine habitual logoff pattern matching baseline."
            ),
        ),
        "clear_roster": (
            "cleared",
            "scout_alt",
            _("[One-Click] Cleared: Incomplete roster / FC administrative omission."),
        ),
        "escalate_senior": (
            "escalated",
            "",
            _(
                "[One-Click] Escalated for senior counterintelligence leadership review."
            ),
        ),
        "dismiss": (
            "dismissed",
            "other",
            _("[One-Click] Dismissed: Non-actionable or noise anomaly."),
        ),
    }

    if action not in action_map:
        messages.error(request, _("Invalid quick disposition action."))
        return redirect("ci:review_queue")

    new_status, res_tag, note = action_map[action]
    investigation.status = new_status
    if res_tag:
        investigation.resolution_tag = res_tag
    if not investigation.assigned_reviewer:
        investigation.assigned_reviewer = request.user

    existing_notes = investigation.reviewer_notes.strip()
    timestamp_str = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    author_str = request.user.username
    entry = f"[{timestamp_str} by {author_str}] {note}"
    investigation.reviewer_notes = (
        f"{existing_notes}\n{entry}".strip() if existing_notes else entry
    )

    # Time-limited CIOverride if cleared/dismissed
    if (
        investigation.status in ("cleared", "dismissed")
        and investigation.resolution_tag
    ):
        CIOverride.create_from_disposition(
            investigation,
            created_by=request.user,
            reason=f"Quick disposition for Investigation #{investigation.id} ({investigation.get_resolution_tag_display()})",
        )

    investigation.save()
    investigation.update_dossier_hash()

    messages.success(
        request,
        _(f"Case #{investigation.id} updated: {investigation.get_status_display()}"),
    )

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "success": True,
                "investigation_id": investigation.id,
                "status": investigation.status,
                "status_display": investigation.get_status_display(),
                "notes": investigation.reviewer_notes,
            }
        )

    next_url = request.POST.get("next") or request.headers.get("referer")
    if next_url:
        return redirect(next_url)
    return redirect("ci:review_queue")


@permission_required("aa_bb.can_manage_ci")
@require_POST
def quick_roster_ingest(request, operation_id: int):
    """
    Ingest fleet roster directly from pasted EVE fleet comp / chat window text.
    Zero administrative overhead, requires no PAP/AFAT plugin.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    raw_text = request.POST.get("roster_text", "").strip()

    if not raw_text:
        messages.warning(request, _("No roster text provided."))
        return redirect("ci:operation_timeline", operation_id=operation.id)

    participants = FCRosterIngestor.ingest_roster_text(
        operation=operation,
        text_data=raw_text,
        fc_user=request.user,
    )
    messages.success(
        request,
        _(
            f"Successfully processed roster: {len(participants)} participant(s) registered."
        ),
    )
    return redirect("ci:operation_timeline", operation_id=operation.id)


def _parse_input_datetime(val: str):
    if not val or not str(val).strip():
        return None
    val = str(val).strip()
    dt = parse_datetime(val)
    if not dt:
        # Standard Library
        from datetime import datetime

        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
        ):
            try:
                dt = datetime.strptime(val, fmt)
                break
            except ValueError:
                continue
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt


def _recalculate_operation_window(operation: CIOperation) -> bool:
    """
    Re-correlate an operation after its window changed and, once zKillboard has
    had time to receive the window's killmails, queue a combat rescan.
    Returns True if a rescan was queued; otherwise the pipeline scans it later.
    """
    CorrelationEngine.correlate_operation(operation)
    _window_start, window_end = CIOperationService.get_operational_window(operation)
    ready_at = window_end + timedelta(minutes=ZKILL_SCAN_DELAY_MINUTES)
    if ready_at > timezone.now():
        return False
    rescan_operation_combat_task.delay(operation.id)
    return True


def _save_window_change(operation: CIOperation, update_fields: list) -> bool:
    # The window changed, so zKillboard must be scanned again, and fleet syncs
    # must not overwrite the manager's times
    operation.combat_scanned_at = None
    operation.window_locked = True
    operation.save(update_fields=[*update_fields, "combat_scanned_at", "window_locked"])
    return _recalculate_operation_window(operation)


def _rescan_message(rescan_queued: bool) -> str:
    if rescan_queued:
        return str(_("A zKillboard rescan of the new window has been queued."))
    return str(_("zKillboard will be scanned 30 minutes after the window closes."))


@permission_required("aa_bb.can_manage_ci")
@require_POST
def edit_operation(request, operation_id: int):
    """
    Edit operational parameters: name, timings, FC, secrecy, notes, or quick-close.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    quick_action = request.POST.get("quick_action")

    if quick_action == "close_now":
        operation.end_time = timezone.now()
        rescan_queued = _save_window_change(operation, ["end_time"])
        messages.success(
            request,
            _(
                f"Operation '{operation.name}' closed at {operation.end_time:%H:%M} UTC. "
                f"{_rescan_message(rescan_queued)}"
            ),
        )
        return redirect("ci:operation_timeline", operation_id=operation.id)

    if quick_action == "reopen":
        operation.end_time = None
        _save_window_change(operation, ["end_time"])
        messages.success(request, _(f"Operation '{operation.name}' reopened."))
        return redirect("ci:operation_timeline", operation_id=operation.id)

    name = request.POST.get("name", "").strip()
    if name:
        operation.name = name
    previous_window = (operation.start_time, operation.end_time)

    operation.operation_type = (
        request.POST.get("operation_type", operation.operation_type).strip()
        or "strategic"
    )
    operation.secrecy_level = (
        request.POST.get("secrecy_level", operation.secrecy_level).strip() or "standard"
    )

    raw_start = request.POST.get("start_time")
    if raw_start:
        parsed_start = _parse_input_datetime(raw_start)
        if parsed_start:
            operation.start_time = parsed_start

    raw_end = request.POST.get("end_time")
    if raw_end is not None:
        if raw_end.strip():
            parsed_end = _parse_input_datetime(raw_end)
            if parsed_end:
                operation.end_time = parsed_end
        else:
            operation.end_time = None

    fc_id = request.POST.get("fleet_commander_id")
    if fc_id:
        try:
            operation.fleet_commander = User.objects.filter(id=int(fc_id)).first()
        except (ValueError, TypeError):
            pass
    elif "fleet_commander_id" in request.POST:
        operation.fleet_commander = None

    if operation.end_time and operation.end_time < operation.start_time:
        messages.error(
            request, _("The end time cannot be earlier than the start time.")
        )
        return redirect("ci:operation_timeline", operation_id=operation.id)

    operation.notes = request.POST.get("notes", "").strip()
    operation.save()

    if (operation.start_time, operation.end_time) != previous_window:
        rescan_queued = _save_window_change(operation, ["start_time", "end_time"])
        messages.success(
            request,
            _(
                f"Operation '{operation.name}' updated; the timeline and findings "
                f"were recalculated for the new window. {_rescan_message(rescan_queued)}"
            ),
        )
    else:
        CorrelationEngine.correlate_operation(operation)
        messages.success(
            request, _(f"Operation '{operation.name}' details updated successfully.")
        )
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def toggle_leak(request, operation_id: int):
    """
    Mark an operation as leaked (hostiles were evidently forewarned) or clear the
    mark, then recompute leak exposure across all leaked operations.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    operation.leaked = not operation.leaked
    operation.leak_notes = (
        request.POST.get("leak_notes", "").strip() if operation.leaked else ""
    )
    operation.save(update_fields=["leaked", "leak_notes"])
    recompute_leak_exposure_task.delay(operation.id)

    if operation.leaked:
        messages.warning(
            request,
            _(
                f"Operation '{operation.name}' marked as leaked. Leak exposure "
                f"is being recalculated across leaked operations."
            ),
        )
    else:
        messages.success(request, _(f"Leak mark cleared on '{operation.name}'."))
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def rescan_combat(request, operation_id: int):
    """
    Re-scan zKillboard for the operation's window immediately and re-correlate.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    events = ZKillIngestor.scan_operation(operation)
    CorrelationEngine.correlate_operation(operation)
    if operation.combat_scanned_at:
        messages.success(
            request,
            _(f"zKillboard scan complete: {len(events)} killmail(s) in window."),
        )
    else:
        messages.warning(
            request,
            _(
                "zKillboard scan was incomplete (rate limit or outage); "
                "it will be retried automatically."
            ),
        )
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def add_objective(request, operation_id: int):
    """
    Add an objective to an operation.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    name = request.POST.get("name", "").strip()
    obj_type = request.POST.get("objective_type", "combat").strip()
    raw_roles = request.POST.get("expected_roles", "").strip()
    raw_disclosure = request.POST.get("disclosure_time", "").strip()

    if not name:
        messages.warning(request, _("Objective name cannot be empty."))
        return redirect("ci:operation_timeline", operation_id=operation.id)

    expected_roles = (
        [r.strip() for r in raw_roles.split(",") if r.strip()] if raw_roles else []
    )
    disclosure_time = _parse_input_datetime(raw_disclosure) if raw_disclosure else None

    CIOperationObjective.objects.create(
        operation=operation,
        name=name,
        objective_type=obj_type,
        expected_roles=expected_roles,
        disclosure_time=disclosure_time,
    )
    messages.success(request, _(f"Objective '{name}' added successfully."))
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def delete_objective(request, operation_id: int, objective_id: int):
    """
    Delete an objective from an operation.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    objective = get_object_or_404(
        CIOperationObjective, id=objective_id, operation=operation
    )
    obj_name = objective.name
    objective.delete()
    messages.success(request, _(f"Objective '{obj_name}' removed."))
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def add_participant(request, operation_id: int):
    """
    Add a single participant by character name or ID to an operation.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    char_input = request.POST.get("character_input", "").strip()

    if not char_input:
        messages.warning(request, _("Please provide a character name or Character ID."))
        return redirect("ci:operation_timeline", operation_id=operation.id)

    parts = FCRosterIngestor.ingest_fc_roster(
        operation=operation,
        roster_entries=[char_input],
        fc_user=request.user,
    )
    if parts:
        messages.success(request, _(f"Participant '{char_input}' registered."))
    else:
        messages.warning(
            request, _(f"Character '{char_input}' registered with provisional ID.")
        )

    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def remove_participant(request, operation_id: int, participant_id: int):
    """
    Remove a participant entry from an operation.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    part = get_object_or_404(
        CIOperationParticipant, id=participant_id, operation=operation
    )
    p_user = part.user.username if part.user else str(part.character_id)
    part.delete()
    messages.success(request, _(f"Participant '{p_user}' removed from FC roster."))
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def auto_detect_comms(request, operation_id: int):
    """
    Auto-detect and confirm fleet participants from communication channels during
    the operation window. Zero extra administrative burden.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    raw_channels = request.POST.get("channel_keywords") or request.POST.get(
        "channel_name", ""
    )
    channel_keywords = (
        [c.strip() for c in raw_channels.split(",") if c.strip()]
        if raw_channels
        else None
    )

    raw_min_dur = request.POST.get("min_duration_seconds", "30")
    try:
        min_dur = max(0, int(raw_min_dur))
    except (ValueError, TypeError):
        min_dur = 30

    participants = FCRosterIngestor.auto_detect_fleet_participants(
        operation=operation,
        channel_keywords=channel_keywords,
        min_duration_seconds=min_dur,
    )
    if participants:
        messages.success(
            request,
            _(
                f"Successfully auto-detected {len(participants)} fleet participant(s) from comms presence."
            ),
        )
    else:
        messages.warning(
            request,
            _(
                "No voice comms activity found for the operational window. Verify Mumble/Discord logs or paste fleet roster instead."
            ),
        )
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def delete_operation(request, operation_id: int):
    """
    Delete an operational window and cascade-clean its associated participants, objectives, and correlations.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    op_name = operation.name
    operation.delete()
    messages.success(request, _(f"Operation '{op_name}' was successfully deleted."))
    return redirect("ci:operation_list")


@permission_required("aa_bb.can_manage_ci")
@require_POST
def deduplicate_operations_view(request):
    """
    Scans and merges duplicate operations sharing the same name and time window.
    """
    merged_count = CIOperationService.deduplicate_operations()
    if merged_count:
        messages.success(
            request,
            _(
                f"Successfully cleaned up and merged {merged_count} duplicate operation(s)."
            ),
        )
    else:
        messages.info(request, _("No duplicate operations found."))
    return redirect("ci:operation_list")
