"""Hook into Alliance Auth"""

# Django
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth import hooks
from allianceauth.services.hooks import MenuItemHook, UrlHook

# AA Example App
from aa_bb import urls, urls_loa, urls_cb, urls_paps
from .models import BigBrotherConfig, LeaveRequest

from .app_settings import afat_active

class CorpBrotherMenuItem(MenuItemHook):
    """This class ensures only authorized users will see the menu entry"""

    def __init__(self):
        """Initialize the sidebar link for CorpBrother."""
        MenuItemHook.__init__(
            self,
            _("Corp Brother"),
            "fas fa-eye fa-fw",
            "aa_cb:index",
            navactive=["aa_cb:"],
        )

    def render(self, request):
        """Render the menu item"""

        try:
            cfg = BigBrotherConfig.get_solo()
        except BigBrotherConfig.DoesNotExist:
            cfg = None

        if request.user.has_perm("aa_bb.basic_access_cb"):  # User has access permission.
            return MenuItemHook.render(self, request)

        return ""


@hooks.register("menu_item_hook")
def register_menu_cb():
    """Register the menu item"""

    return CorpBrotherMenuItem()


@hooks.register("url_hook")
def register_corpbrother_urls():
    """Expose the CorpBrother URLconf to AllianceAuth."""
    return UrlHook(urls_cb, "CorpBrother", r"^aa_cb/")


class BigBrotherMenuItem(MenuItemHook):
    """This class ensures only authorized users will see the menu entry"""

    def __init__(self):
        """Initialize the sidebar link for BigBrother core."""
        MenuItemHook.__init__(
            self,
            _("Big Brother"),
            "fas fa-eye fa-fw",
            "aa_bb:index",
            navactive=["aa_bb:index"],
        )

    def render(self, request):
        """Render the menu item"""

        if request.user.has_perm("aa_bb.basic_access"):  # Only show to approved members.
            return MenuItemHook.render(self, request)

        return ""


@hooks.register("menu_item_hook")
def register_menu():
    """Register the menu item"""

    return BigBrotherMenuItem()


class BigBrotherManualMenuItem(MenuItemHook):
    """Menu entry for the BigBrother user manual."""

    def __init__(self):
        """Initialize the manual menu entry with all nav targets."""
        super().__init__(
            _("Big Brother Manual"),
            "fas fa-book",
            "aa_bb:manual",
            navactive=[
                "aa_bb:manual",
                "aa_bb:manual_cards",
                "aa_bb:manual_settings",
                "aa_bb:manual_settings_bb",
                "aa_bb:manual_settings_paps",
                "aa_bb:manual_settings_tickets",
                "aa_bb:manual_modules",
                "aa_bb:manual_faq",
            ],
        )

    def render(self, request):
        """Show manual link for anyone with standard BB access."""
        if request.user.has_perm("aa_bb.basic_access"):  # Manual mirrors BB permissions.
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_bigbrother_manual_menu():
    """Register the BB manual sidebar entry."""
    return BigBrotherManualMenuItem()


@hooks.register("url_hook")
def register_bigbrother_urls():
    """Expose the BigBrother URLconf to AllianceAuth."""
    return UrlHook(urls, "BigBrother", r"^aa_bb/")


class LoAMenuItem(MenuItemHook):
    """Menu entry for Leave of Absence tools, gated by permissions."""
    def __init__(self):
        """Initialize the LoA entry and nav state."""
        super().__init__(
            _("Leave of Absence"),
            "fas fa-plane",
            "loa:index",
            navactive=["loa:"],
    )
    def render(self, request):
        """Show LoA entry when permission is active."""
        if request.user.has_perm("aa_bb.can_access_loa"):  # Basic LoA access.
            if request.user.has_perm("aa_bb.can_view_all_loa"):  # Staff can see pending counters.
                pending_count = LeaveRequest.objects.filter(status="pending").count()
                if pending_count:  # Highlight nav badge when there are pending requests.
                    self.count = pending_count
                return MenuItemHook.render(self, request)
            return MenuItemHook.render(self, request)
        return ""

@hooks.register("menu_item_hook")
def register_loa_menu():
    """Register the LOA sidebar entry."""
    return LoAMenuItem()

@hooks.register("url_hook")
def register_loa_urls():
    """Expose LOA URLs to AllianceAuth."""
    return UrlHook(urls_loa, "loa", r"^loa/")


class TicketManagerMenuItem(MenuItemHook):
    """Sidebar entry for ticket managers."""
    def __init__(self):
        super().__init__(
            _("Compliance Tickets"),
            "fas fa-ticket-alt",
            "aa_bb:ticket_list",
            navactive=["aa_bb:ticket_list", "aa_bb:ticket_view"],
        )

    def render(self, request):
        """Show only to ticket managers."""
        if request.user.has_perm("aa_bb.ticket_manager"):
            from .models import ComplianceTicket
            from .app_settings import afat_active, discordbot_active
            # Only count tickets that are open (not resolved and not exception)
            qs = ComplianceTicket.objects.filter(is_resolved=False, is_exception=False)
            if not afat_active():
                qs = qs.exclude(reason="paps_check")
            if not discordbot_active():
                qs = qs.exclude(reason="discord_check")
            count = qs.count()
            if count:
                self.count = count
            return super().render(request)
        return ""


@hooks.register("menu_item_hook")
def register_ticket_manager_menu():
    """Register the ticket management menu entry."""
    return TicketManagerMenuItem()


class PapsMenuItem(MenuItemHook):
    """Menu entry for PAP statistics, only shown when the module is active."""
    def __init__(self):
        """Initialize the PAP menu entry and nav state."""
        super().__init__(
            _("PAP Stats"),
            "fas fa-chart-bar",
            "paps:history",
            navactive=["paps:"],
    )
    def render(self, request):
        """Only show when users have permission and AFAT is active."""
        if not afat_active():
            return ""
        if request.user.has_perm("aa_bb.can_access_paps"):  # Only show for PAP viewers.
            return super().render(request)
        return ""

if afat_active():
    @hooks.register("menu_item_hook")
    def register_paps_menu():
        """Register the PAP stats sidebar entry if AFAT is active."""
        return PapsMenuItem()

if afat_active():
    @hooks.register("url_hook")
    def register_paps_urls():
        """Wire the PAP URLconf into AllianceAuth."""
        return UrlHook(urls_paps, "paps", r"^paps/")


@hooks.register('discord_cogs_hook')
def register_cogs():
    """Ensure aa_bb's Discord tasks cog is loaded by the bot runner."""
    from .app_settings import discordbot_active
    if discordbot_active():
        return ["aa_bb.tasks_bot"]
    return []
