"""App URLs"""

from django.urls import path
from aa_bb import views, views_faq

app_name = "aa_bb"

urlpatterns = [
    # Main index view
    path("", views.index, name="index"),  # Landing page showing current dashboard cards.
    path("manual/", views_faq.manual_cards, name="manual"),  # Top-level manual hub (redirects to cards).
    path("manual/cards/", views_faq.manual_cards, name="manual_cards"),  # FAQ cards describing each module.
    path("manual/settings/", views_faq.manual_settings, name="manual_settings"),  # Manual section listing module settings.
    path(
        "manual/settings/bigbrother/",
        views_faq.manual_settings_bb,
        name="manual_settings_bb",
    ),  # BigBrother specific settings reference page.
    path(
        "manual/settings/paps/",
        views_faq.manual_settings_paps,
        name="manual_settings_paps",
    ),  # PAPs module explanation/settings.
    path(
        "manual/settings/tickets/",
        views_faq.manual_settings_tickets,
        name="manual_settings_tickets",
    ),  # Ticket module documentation.
    path(
        "manual/settings/stats/",
        views_faq.manual_settings_stats,
        name="manual_settings_stats",
    ),  # Recurring stats documentation.
    path("manual/modules/", views_faq.manual_modules, name="manual_modules"),  # Landing page for module-specific docs.
    path("manual/faq/", views_faq.manual_faq, name="manual_faq"),  # General FAQ/guide landing page.

    # Bulk loader
    path("load_cards/", views.load_cards, name="load_cards"),  # Legacy endpoint.

    # Single card AJAX fetch (all cards except paging for SUS_CONTR)
    path("load_card/", views.load_card, name="load_card"),  # Fetch one card’s HTML payload on-demand.
    path("warm_cache/", views.warm_cache, name="warm_cache"),  # Trigger backend warm-up of cached card data.
    path("warm-progress/", views.get_warm_progress, name="warm_progress"),  # Poll for warm-up job status.

    # Suspicious Contracts streaming fallback (if desired)
    path("stream_contracts_sse/", views.stream_contracts_sse, name="stream_contracts_sse"),  # SSE feed for contracts.
    path("stream_mails_sse/", views.stream_mails_sse, name="stream_mails_sse"),  # SSE feed for suspicious mails.
    path("stream_transactions_sse/", views.stream_transactions_sse, name="stream_transactions_sse"),  # SSE feed for wallet transactions.
    path("stream_assets_sse/", views.stream_assets_sse, name="stream_assets_sse"),
    path("stream_clones_sse/", views.stream_clones_sse, name="stream_clones_sse"),
    path("stream_contacts_sse/", views.stream_contacts_sse, name="stream_contacts_sse"),

    # Paginated Suspicious Contracts endpoints
    path("list_contract_ids/", views.list_contract_ids, name="list_contract_ids"),  # Provide IDs for contract pagination.
    path("check_contract_batch/", views.check_contract_batch, name="check_contract_batch"),  # Fetch specific contract batch details.

    # Blacklist management
    path("blacklist/add/", views.add_blacklist_view, name="add_blacklist"),  # Simple UI to add entries to corp blacklist.

    # Ticket Management UI
    path("tickets/", views.ticket_list, name="ticket_list"),
    path("tickets/<int:pk>/", views.ticket_view, name="ticket_view"),
    path("tickets/<int:pk>/resolve/", views.ticket_resolve, name="ticket_resolve"),
    path("tickets/<int:pk>/reopen/", views.ticket_reopen, name="ticket_reopen"),
    path("tickets/<int:pk>/mark_exception/", views.ticket_mark_exception, name="ticket_mark_exception"),
    path("tickets/<int:pk>/clear_exception/", views.ticket_clear_exception, name="ticket_clear_exception"),
    path("tickets/<int:pk>/comment/", views.ticket_add_comment, name="ticket_add_comment"),
]
