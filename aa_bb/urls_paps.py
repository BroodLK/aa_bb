# Django
from django.urls import path

from . import views_paps

app_name = "paps"

urlpatterns = [
    path("generate/", views_paps.index, name="index"),  # Entry point to generate PAP summaries.
    path("", views_paps.history, name="history"),  # Default view lists historical PAP exports.
    path(
        "generate-chart/", views_paps.generate_pap_chart, name="generate_pap_chart"
    ),  # Serve or build the PAP chart image/data.
]
