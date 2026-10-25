"""
Counterintelligence views and dashboards.
"""

# Django
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

# AA BigBrother
from aa_bb.models import BigBrotherConfig, UserStatus

from .ingest.paps import FCRosterIngestor
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
from .services.agent import CIAgentService
from .services.baseline import BaselineEngine
from .services.correlation import CorrelationEngine
from .services.operations import CIOperationService


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

    return render(
        request,
        "aa_bb/counterintel/operation_list.html",
        {
            "page_obj": page_obj,
            "title": _("Counterintelligence - Operations"),
        },
    )


@permission_required("aa_bb.can_view_ci")
def operation_timeline(request, operation_id: int):
    """
    Detailed operational timeline dashboard reconciling comms, roster, and combat.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    timeline = CIOperationService.reconstruct_timeline(operation)

    if (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.GET.get("format") == "json"
    ):
        return JsonResponse(timeline, safe=False)

    cfg = BigBrotherConfig.get_solo()
    member_states = cfg.bb_member_states.all()
    fc_candidates = (
        User.objects.filter(is_active=True, profile__state__in=member_states)
        .select_related("profile__main_character")
        .order_by("username")[:200]
    )

    return render(
        request,
        "aa_bb/counterintel/operation_timeline.html",
        {
            "operation": operation,
            "timeline": timeline,
            "fc_candidates": fc_candidates,
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
        .order_by("username")
        .distinct()
    )
    search_q = request.GET.get("q", "").strip()
    if search_q:
        users = users.filter(username__icontains=search_q)

    paginator = Paginator(users, 30)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "aa_bb/counterintel/member_list.html",
        {
            "page_obj": page_obj,
            "search_q": search_q,
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
            "participations": participations,
            "correlations": correlations,
            "overrides": overrides,
            "user_status": user_status,
            "last_discord_chat": last_discord_chat,
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
        "subject_user", "operation", "assigned_reviewer"
    ).order_by("-created_at")

    if status_filter and status_filter != "all":
        investigations = investigations.filter(status=status_filter)

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

        # False-Positive Feedback Loop: auto-create scoped CIOverride if cleared/dismissed with tag
        if (
            investigation.status in ("cleared", "dismissed")
            and investigation.resolution_tag
        ):
            exempt_types = (
                list(
                    CICorrelation.objects.filter(
                        user=investigation.subject_user,
                        operation=investigation.operation,
                    ).values_list("anomaly_type", flat=True)
                )
                if investigation.operation
                else []
            )

            if exempt_types:
                CIOverride.objects.get_or_create(
                    user=investigation.subject_user,
                    reason=f"Auto-generated from Investigation #{investigation.id} ({investigation.get_resolution_tag_display()})",
                    defaults={
                        "exempt_anomaly_types": exempt_types,
                        "created_by": request.user,
                    },
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
        timeline = CIOperationService.reconstruct_timeline(investigation.operation)

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
    Alliance-wide Member Risk Matrix Dashboard displaying composite risk index,
    anomaly count, fleet attendance, and communication engagement.
    """
    cfg = BigBrotherConfig.get_solo()
    member_states = cfg.bb_member_states.all()
    users_qs = (
        User.objects.filter(is_active=True, profile__state__in=member_states)
        .select_related("profile__main_character", "userstatus")
        .prefetch_related("ci_correlations")
        .distinct()
    )

    search_q = request.GET.get("q", "").strip()
    tier_filter = request.GET.get("tier", "").strip().upper()

    if search_q:
        users_qs = users_qs.filter(username__icontains=search_q)

    # Calculate summaries
    all_members_data = []
    high_risk_count = 0
    med_risk_count = 0
    low_risk_count = 0

    for u in users_qs:
        corrs = list(u.ci_correlations.all())
        anomaly_count = len(corrs)
        max_corr_risk = max([c.risk_score for c in corrs], default=0.0)

        # Baseline & factors
        risk_score, factors = BaselineEngine.calculate_user_risk_score(
            u,
            findings=[
                {"anomaly_type": c.anomaly_type, "confidence": c.confidence}
                for c in corrs
            ],
        )
        effective_risk = max(risk_score, max_corr_risk)

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

        main_char = getattr(getattr(u, "profile", None), "main_character", None)
        last_discord = UserStatus.get_last_discord_message_at_for_user(u)

        all_members_data.append(
            {
                "user": u,
                "main_character": main_char,
                "state": getattr(getattr(u, "profile", None), "state", None),
                "risk_score": round(effective_risk, 2),
                "risk_tier": tier,
                "risk_class": tier_class,
                "anomaly_count": anomaly_count,
                "last_discord": last_discord,
                "factors": factors,
            }
        )

    # Sort descending by risk score
    all_members_data.sort(
        key=lambda x: (x["risk_score"], x["anomaly_count"]), reverse=True
    )

    paginator = Paginator(all_members_data, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(
        request,
        "aa_bb/counterintel/risk_matrix.html",
        {
            "page_obj": page_obj,
            "search_q": search_q,
            "tier_filter": tier_filter,
            "high_risk_count": high_risk_count,
            "med_risk_count": med_risk_count,
            "low_risk_count": low_risk_count,
            "total_members": len(all_members_data),
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

    # Auto-create scoped CIOverride if cleared/dismissed
    if (
        investigation.status in ("cleared", "dismissed")
        and investigation.resolution_tag
    ):
        exempt_types = (
            list(
                CICorrelation.objects.filter(
                    user=investigation.subject_user,
                    operation=investigation.operation,
                ).values_list("anomaly_type", flat=True)
            )
            if investigation.operation
            else []
        )

        if exempt_types:
            CIOverride.objects.get_or_create(
                user=investigation.subject_user,
                reason=f"Quick disposition for Investigation #{investigation.id} ({investigation.get_resolution_tag_display()})",
                defaults={
                    "exempt_anomaly_types": exempt_types,
                    "created_by": request.user,
                },
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
        operation.save(update_fields=["end_time"])
        CorrelationEngine.correlate_operation(operation)
        messages.success(
            request,
            _(
                f"Operation '{operation.name}' closed at {operation.end_time:%H:%M} UTC."
            ),
        )
        return redirect("ci:operation_timeline", operation_id=operation.id)

    if quick_action == "reopen":
        operation.end_time = None
        operation.save(update_fields=["end_time"])
        CorrelationEngine.correlate_operation(operation)
        messages.success(request, _(f"Operation '{operation.name}' reopened."))
        return redirect("ci:operation_timeline", operation_id=operation.id)

    name = request.POST.get("name", "").strip()
    if name:
        operation.name = name

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

    operation.notes = request.POST.get("notes", "").strip()

    operation.save()
    CorrelationEngine.correlate_operation(operation)

    messages.success(
        request, _(f"Operation '{operation.name}' details updated successfully.")
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
