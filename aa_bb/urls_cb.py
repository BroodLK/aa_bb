"""App URLs"""

from django.urls import path
from aa_bb import views_cb as views

app_name = "aa_cb"

urlpatterns = [
    # Main index view
    path("",                 views.index,                  name="index"),  # CorpBrother dashboard root.

    # Bulk loader
    path("load_cards/",      views.load_cards,             name="load_cards"),  # Legacy full-card refresh endpoint.

    # Single card AJAX fetch (all cards except paging for SUS_CONTR)
    path("load_card/",       views.load_card,              name="load_card"),  # Load a single CorpBrother card payload.
    path("warm_cache/", views.warm_cache, name="warm_cache"),  # Begin cache warm-up for CB data.
    path('warm-progress/', views.get_warm_progress, name='warm_progress'),  # Poll warm-up progress for CB data.

    # Suspicious Contracts streaming fallback (if desired)
    path('stream_contracts_sse/', views.stream_contracts_sse, name='stream_contracts_sse'),  # SSE feed for CB contracts.
    path('stream_transactions_sse/', views.stream_transactions_sse, name='stream_transactions_sse'),  # SSE feed for CB wallet transactions.
    path('stream_assets_sse/', views.stream_assets_sse, name='stream_assets_sse'),

    # Paginated Suspicious Contracts endpoints
    path('list_contract_ids/', views.list_contract_ids,       name='list_contract_ids'),  # Contract pagination helper.
    path('check_contract_batch/', views.check_contract_batch, name='check_contract_batch'),  # Fetch detailed contract batch data.
]
