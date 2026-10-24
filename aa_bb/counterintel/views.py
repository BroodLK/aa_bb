"""
Counterintelligence views and dashboards.
"""

# Django
from django.contrib import messages
from django.contrib.auth.decorators import permission_required
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from django.views.decorators.http import require_POST

from .ingest.paps import FCRosterIngestor
from .models import (
    CICorrelation,
    CIEvent,
    CIIdentity,
    CIInvestigation,
    CIOperation,
    CIOperationParticipant,
    CIOverride,
    CISession,
)
from .services.agent import CIAgentService
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

    if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.GET.get("format") == "json":
        return JsonResponse(timeline, safe=False)

    return render(
        request,
        "aa_bb/counterintel/operation_timeline.html",
        {
            "operation": operation,
            "timeline": timeline,
            "title": f"{operation.name} - {_('Operation Timeline')}",
        },
    )


@permission_required("aa_bb.can_view_ci")
def member_list(request):
    """
    Directory of tracked members with communication sessions and correlation findings.
    """
    users = User.objects.filter(is_active=True).order_by("username")
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
    subject_user = get_object_or_404(User, id=user_id)
    identities = CIIdentity.objects.filter(user=subject_user).order_by("-effective_from")
    sessions = CISession.objects.filter(user=subject_user).order_by("-start_time")[:50]
    participations = CIOperationParticipant.objects.filter(user=subject_user).select_related("operation").order_by("-recorded_at")[:50]
    correlations = CICorrelation.objects.filter(user=subject_user).select_related("operation").order_by("-computed_at")
    overrides = CIOverride.objects.filter(user=subject_user).order_by("-created_at")

    return render(
        request,
        "aa_bb/counterintel/member_activity.html",
        {
            "subject_user": subject_user,
            "identities": identities,
            "sessions": sessions,
            "participations": participations,
            "correlations": correlations,
            "overrides": overrides,
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
        CIInvestigation.objects.select_related("subject_user", "operation", "assigned_reviewer"),
        id=investigation_id,
    )

    if request.method == "POST":
        new_status = request.POST.get("status")
        reviewer_notes = request.POST.get("reviewer_notes", "").strip()

        if new_status in dict(CIInvestigation.STATUS_CHOICES):
            investigation.status = new_status
        if reviewer_notes:
            investigation.reviewer_notes = reviewer_notes
        if not investigation.assigned_reviewer:
            investigation.assigned_reviewer = request.user

        investigation.save()
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
            "title": f"Investigation - {investigation.subject_user.username}",
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
            _("[One-Click] Cleared: Verified Alt / Scout / Support operational role."),
        ),
        "clear_habitual": (
            "cleared",
            _("[One-Click] Cleared: Routine habitual logoff pattern matching baseline."),
        ),
        "clear_roster": (
            "cleared",
            _("[One-Click] Cleared: Incomplete roster / FC administrative omission."),
        ),
        "escalate_senior": (
            "escalated",
            _("[One-Click] Escalated for senior counterintelligence leadership review."),
        ),
        "dismiss": (
            "dismissed",
            _("[One-Click] Dismissed: Non-actionable or noise anomaly."),
        ),
    }

    if action not in action_map:
        messages.error(request, _("Invalid quick disposition action."))
        return redirect("ci:review_queue")

    new_status, note = action_map[action]
    investigation.status = new_status
    if not investigation.assigned_reviewer:
        investigation.assigned_reviewer = request.user

    existing_notes = investigation.reviewer_notes.strip()
    timestamp_str = timezone.now().strftime("%Y-%m-%d %H:%M UTC")
    author_str = request.user.username
    entry = f"[{timestamp_str} by {author_str}] {note}"
    investigation.reviewer_notes = f"{existing_notes}\n{entry}".strip() if existing_notes else entry
    investigation.save()

    messages.success(request, _(f"Case #{investigation.id} updated: {investigation.get_status_display()}"))

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse({
            "success": True,
            "investigation_id": investigation.id,
            "status": investigation.status,
            "status_display": investigation.get_status_display(),
            "notes": investigation.reviewer_notes,
        })

    next_url = request.POST.get("next") or request.META.get("HTTP_REFERER")
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
        _(f"Successfully processed roster: {len(participants)} participant(s) registered."),
    )
    return redirect("ci:operation_timeline", operation_id=operation.id)


@permission_required("aa_bb.can_manage_ci")
@require_POST
def auto_detect_comms(request, operation_id: int):
    """
    Auto-detect and confirm fleet participants from communication channels during
    the operation window. Zero extra administrative burden.
    """
    operation = get_object_or_404(CIOperation, id=operation_id)
    participants = FCRosterIngestor.auto_detect_fleet_participants(operation=operation)
    messages.success(
        request,
        _(f"Auto-detected {len(participants)} fleet participant(s) from comms presence."),
    )
    return redirect("ci:operation_timeline", operation_id=operation.id)
