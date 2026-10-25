"""
Counterintelligence URL configuration.
"""

# Django
from django.urls import path

from . import views

app_name = "ci"

urlpatterns = [
    path("", views.operation_list, name="index"),
    path("operations/", views.operation_list, name="operation_list"),
    path(
        "operations/<int:operation_id>/",
        views.operation_timeline,
        name="operation_timeline",
    ),
    path(
        "operations/<int:operation_id>/edit/",
        views.edit_operation,
        name="edit_operation",
    ),
    path(
        "operations/<int:operation_id>/delete/",
        views.delete_operation,
        name="delete_operation",
    ),
    path(
        "operations/deduplicate/",
        views.deduplicate_operations_view,
        name="deduplicate_operations",
    ),
    path(
        "operations/<int:operation_id>/objectives/add/",
        views.add_objective,
        name="add_objective",
    ),
    path(
        "operations/<int:operation_id>/objectives/<int:objective_id>/delete/",
        views.delete_objective,
        name="delete_objective",
    ),
    path(
        "operations/<int:operation_id>/participants/add/",
        views.add_participant,
        name="add_participant",
    ),
    path(
        "operations/<int:operation_id>/participants/<int:participant_id>/remove/",
        views.remove_participant,
        name="remove_participant",
    ),
    path(
        "operations/<int:operation_id>/quick-roster/",
        views.quick_roster_ingest,
        name="quick_roster_ingest",
    ),
    path(
        "operations/<int:operation_id>/auto-detect-comms/",
        views.auto_detect_comms,
        name="auto_detect_comms",
    ),
    path("members/", views.member_list, name="member_list"),
    path("members/<int:user_id>/", views.member_activity, name="member_activity"),
    path("members/<int:user_id>/dossier/", views.member_dossier, name="member_dossier"),
    path("risk-matrix/", views.risk_matrix, name="risk_matrix"),
    path("review/", views.review_queue, name="review_queue"),
    path(
        "review/<int:investigation_id>/",
        views.investigation_detail,
        name="investigation_detail",
    ),
    path(
        "review/<int:investigation_id>/export/",
        views.export_investigation_dossier,
        name="export_investigation_dossier",
    ),
    path(
        "review/<int:investigation_id>/quick-disposition/",
        views.quick_disposition,
        name="quick_disposition",
    ),
]
