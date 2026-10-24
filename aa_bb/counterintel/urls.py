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
    path("review/", views.review_queue, name="review_queue"),
    path(
        "review/<int:investigation_id>/",
        views.investigation_detail,
        name="investigation_detail",
    ),
    path(
        "review/<int:investigation_id>/quick-disposition/",
        views.quick_disposition,
        name="quick_disposition",
    ),
]
