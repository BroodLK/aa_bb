# Django
from django.urls import path

from . import views

app_name = "loa"  # This is the LoA namespace

urlpatterns = [
    path("", views.loa_loa, name="index"),  # Main LoA landing page listing current requests.
    path("admin/", views.loa_admin, name="admin"),  # Staff panel for reviewing and managing LoAs.
    path("request/", views.loa_request, name="request"),  # Form for members to submit new LoA requests.
    path("delete/<int:pk>/", views.delete_request, name="delete_request"),  # Allow requester to delete their own LoA.
    path(
        "deleteadmin/<int:pk>/", views.delete_request_admin, name="delete_request_admin"
    ),  # Admin override delete route.
    path("approve/<int:pk>/", views.approve_request, name="approve_request"),  # Approve an LoA request.
    path("deny/<int:pk>/", views.deny_request, name="deny_request"),  # Deny an LoA request with notice.
]
