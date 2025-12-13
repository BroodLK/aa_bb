"""
Admin registrations for every BigBrother-related model.

Most models are singletons that gate optional DLC modules. The helpers below
ensure their admin entries only appear when the relevant feature is enabled
and prevent accidental multi-row creation of what should be one-off configs.
"""

from solo.admin import SingletonModelAdmin

from django.contrib import admin

from .models import (
    BigBrotherConfig,
    Messages,
    OptMessages1,
    OptMessages2,
    OptMessages3,
    OptMessages4,
    OptMessages5,
    UserStatus,
    WarmProgress,
    PapsConfig,
    RecurringStatsConfig,
)
from .modelss import (
    TicketToolConfig,
    PapCompliance,
    LeaveRequest,
    ComplianceTicket,
    BigBrotherRedditSettings,
    BigBrotherRedditMessage,
)
from .reddit import is_reddit_module_visible
from django.core.exceptions import ObjectDoesNotExist


class DLCVisibilityMixin:
    """Hide admin entries when the related DLC flag is disabled."""

    dlc_attr = None

    def _allowed(self) -> bool:
        """Return True when the DLC attribute is enabled or not required."""
        if not self.dlc_attr:  # Always allow when no DLC flag is configured.
            return True
        try:
            cfg = BigBrotherConfig.get_solo()
        except ObjectDoesNotExist:
            return False
        return bool(getattr(cfg, self.dlc_attr, False))

    def has_module_permission(self, request):
        """Hide the entire admin module when the DLC is disabled."""
        return self._allowed() and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        """Disable read access when the DLC is disabled."""
        return self._allowed() and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        """Disable add operations when the DLC is disabled."""
        return self._allowed() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        """Disable edit operations when the DLC is disabled."""
        return self._allowed() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        """Disable delete operations when the DLC is disabled."""
        return self._allowed() and super().has_delete_permission(request, obj)


class PapModuleVisibilityMixin(DLCVisibilityMixin):
    """Restrict admin entries to installs with the PAP DLC."""
    dlc_attr = "dlc_pap_active"


class TicketModuleVisibilityMixin(DLCVisibilityMixin):
    """Restrict admin entries to installs with the ticketing DLC."""
    dlc_attr = "dlc_tickets_active"


class LoaModuleVisibilityMixin(DLCVisibilityMixin):
    """Show admin pages only when the LoA module is enabled."""
    dlc_attr = "dlc_loa_active"


class DailyMessagesVisibilityMixin(DLCVisibilityMixin):
    """Hide daily/optional message models when that DLC is off."""
    dlc_attr = "dlc_daily_messages_active"


class RecurringStatsVisibilityMixin(DLCVisibilityMixin):
    """Hide recurring stats models when that DLC is off."""
    dlc_attr = "dlc_are_recurring_stats_active"


@admin.register(BigBrotherConfig)
class BB_ConfigAdmin(SingletonModelAdmin):
    fieldsets = (
        (
            "Core Activation",
            {
                "fields": (
                    "is_active",
                    "is_warmer_active",
                    "is_loa_active",
                    "is_paps_active",
                    "are_daily_messages_active",
                    "are_recurring_stats_active",
                    "are_opt_messages1_active",
                    "are_opt_messages2_active",
                    "are_opt_messages3_active",
                    "are_opt_messages4_active",
                    "are_opt_messages5_active",
                    "loa_max_logoff_days",
                )
            },
        ),
        (
            "DLC Flags",
            {
                "classes": ("collapse",),
                "fields": (
                    "dlc_corp_brother_active",
                    "dlc_loa_active",
                    "dlc_pap_active",
                    "dlc_tickets_active",
                    "dlc_reddit_active",
                    "dlc_daily_messages_active",
                    "dlc_are_recurring_stats_active",
                ),
            },
        ),
        (
            "Notifications",
            {
                "fields": (
                    "ct_notify",
                    "awox_notify",
                    "cyno_notify",
                    "sp_inject_notify",
                    "clone_notify",
                    "asset_notify",
                    "contact_notify",
                    "contract_notify",
                    "mail_notify",
                    "transaction_notify",
                    "new_user_notify",
                ),
            },
        ),
        (
            "Ping / Messaging Roles",
            {
                "fields": (
                    "pingroleID",
                    "pingroleID2",
                    "pingrole1_messages",
                    "pingrole2_messages",
                    "here_messages",
                    "everyone_messages",
                )
            },
        ),
        (
            "Webhooks",
            {
                "fields": (
                    "webhook",
                    "loawebhook",
                    "dailywebhook",
                    "optwebhook1",
                    "optwebhook2",
                    "optwebhook3",
                    "optwebhook4",
                    "optwebhook5",
                    "stats_webhook",
                )
            },
        ),
        (
            "Schedules",
            {
                "fields": (
                    "dailyschedule",
                    "optschedule1",
                    "optschedule2",
                    "optschedule3",
                    "optschedule4",
                    "optschedule5",
                    "stats_schedule",
                ),
            },
        ),
        (
            "User State & Membership",
            {
                "fields": (
                    "bb_guest_states",
                    "bb_member_states",
                    "member_corporations",
                    "member_alliances",
                    "ignored_corporations",
                )
            },
        ),
        (
            "Hostile / Whitelist Rules",
            {
                "fields": (
                    "hostile_alliances",
                    "hostile_corporations",
                    "whitelist_alliances",
                    "whitelist_corporations",
                    "consider_nullsec_hostile",
                    "consider_all_structures_hostile",
                    "consider_npc_stations_hostile",
                    "excluded_systems",
                    "excluded_stations",
                    "hostile_assets_ships_only",
                )
            },
        ),
        (
            "Scopes",
            {
                "classes": ("collapse",),
                "fields": (
                    "character_scopes",
                    "corporation_scopes",
                ),
            },
        ),
        (
            "Main Corp / Alliance",
            {
                "fields": (
                    "main_corporation_id",
                    "main_corporation",
                    "main_alliance_id",
                    "main_alliance",
                ),
            },
        ),
    )
    """Singleton config for the core BigBrother module."""
    readonly_fields = (
        "main_corporation",
        "main_alliance",
        "main_corporation_id",
        "main_alliance_id",
        "is_active",
        "dlc_corp_brother_active",
        "dlc_loa_active",
        "dlc_pap_active",
        "dlc_tickets_active",
        "dlc_reddit_active",
        "dlc_daily_messages_active",
        "dlc_are_recurring_stats_active",
    )
    filter_horizontal = (
        "pingrole1_messages",
        "pingrole2_messages",
        "here_messages",
        "everyone_messages",
        "bb_guest_states",
        "bb_member_states",
    )

    def has_add_permission(self, request):
        """Prevent duplicate singleton rows."""
        if BigBrotherConfig.objects.exists():  # Disallow when a config already exists.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Always allow delete to keep parity with default behavior."""
        return True


@admin.register(PapsConfig)
class PapsConfigAdmin(PapModuleVisibilityMixin, SingletonModelAdmin):
    """Controls PAP multipliers/thresholds; singleton per install."""
    filter_horizontal = (
        "group_paps",
        "excluded_groups",
        "excluded_users",
        "excluded_users_paps",
    )

    def has_add_permission(self, request):
        """Prevent duplicate PAP config entries."""
        if PapsConfig.objects.exists():  # Disallow singleton duplication.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Allow deletes so admins can rebuild the configuration."""
        return True


@admin.register(TicketToolConfig)
class TicketToolConfigAdmin(TicketModuleVisibilityMixin, SingletonModelAdmin):
    """Ticket automation thresholds + templates."""
    filter_horizontal = (
        "excluded_users",
    )

    def has_add_permission(self, request):
        """Prevent duplicate ticket config entries."""
        if PapsConfig.objects.exists():  # Ticket config should remain singleton.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Allow deletes when operators need to reset settings."""
        return True


class RedditAdminVisibilityMixin(DLCVisibilityMixin):
    """Hide Reddit admin entries unless DLC + feature flag are active."""
    dlc_attr = "dlc_reddit_active"

    def _allowed(self) -> bool:
        """Require both DLC activation and reddit module visibility."""
        return super()._allowed() and is_reddit_module_visible()


@admin.register(BigBrotherRedditSettings)
class BigBrotherRedditSettingsAdmin(RedditAdminVisibilityMixin, SingletonModelAdmin):
    """OAuth tokens + scheduling info for the Reddit autoposter."""
    exclude = (
        "reddit_access_token",
        "reddit_refresh_token",
        "reddit_token_type",
        "last_submission_id",
        "last_submission_permalink",
        "reddit_account_name",
    )
    readonly_fields = (
        "reddit_token_obtained",
        "last_submission_at",
        "last_reply_checked_at",
        "reddit_account_name",
    )

    def has_add_permission(self, request):
        """Limit the settings model to a single row."""
        if BigBrotherRedditSettings.objects.exists():  # Disallow duplicate settings.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Never allow deleting the Reddit credentials row."""
        return False


@admin.register(BigBrotherRedditMessage)
class BigBrotherRedditMessageAdmin(RedditAdminVisibilityMixin, admin.ModelAdmin):
    """Manage the pool of canned Reddit ads."""
    list_display = ("title", "used_in_cycle", "created")
    list_filter = ("used_in_cycle",)
    search_fields = ("title", "content")


@admin.register(Messages)
class DailyMessageConfig(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Standard daily webhook messages rotated each cycle."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages1)
class OptMessage1Config(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Optional webhook stream #1."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages2)
class OptMessage2Config(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Optional webhook stream #2."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages3)
class OptMessage3Config(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Optional webhook stream #3."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages4)
class OptMessage4Config(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Optional webhook stream #4."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages5)
class OptMessage5Config(DailyMessagesVisibilityMixin, admin.ModelAdmin):
    """Optional webhook stream #5."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(WarmProgress)
class WarmProgressConfig(admin.ModelAdmin):
    """Shows which users the cache warmer has processed recently."""
    list_display = ["user_main", "updated"]


@admin.register(UserStatus)
class UserStatusConfig(admin.ModelAdmin):
    """Simple heartbeat for per-user card status."""
    list_display = ["user", "updated"]


@admin.register(ComplianceTicket)
class ComplianceTicketConfig(TicketModuleVisibilityMixin, admin.ModelAdmin):
    """History of tickets issued by the automation layer."""
    list_display = ["user", "ticket_id", "reason"]


@admin.register(LeaveRequest)
class LeaveRequestConfig(LoaModuleVisibilityMixin, admin.ModelAdmin):
    """Expose LeaveRequest records to staff when LoA is enabled."""
    list_display = ["main_character", "start_date", "end_date", "reason", "status"]


@admin.register(PapCompliance)
class PapComplianceConfig(PapModuleVisibilityMixin, admin.ModelAdmin):
    """Shows the most recent PAP compliance calculation per user."""
    search_fields = ["user_profile"]
    list_display = ["user_profile", "pap_compliant"]


@admin.register(RecurringStatsConfig)
class RecurringStatsConfigAdmin(SingletonModelAdmin):
    fieldsets = (
        (
            "General",
            {
                "fields": ("enabled",),
            },
        ),
        (
            "States",
            {
                "fields": ("states",),
                "description": "Select which states you want broken out (Member, Blue, Alumni, etc.).",
            },
        ),
        (
            "Included Stats",
            {
                "fields": (
                    "include_auth_users",
                    "include_discord_users",
                    "include_mumble_users",
                    "include_characters",
                    "include_corporations",
                    "include_alliances",
                    "include_tokens",
                    "include_unique_tokens",
                    "include_character_audits",
                    "include_corporation_audits",
                ),
            },
        ),
        (
            "Internal",
            {
                "fields": ("last_run_at", "last_snapshot"),
                "classes": ("collapse",),
            },
        ),
    )

    filter_horizontal = ("states",)
    readonly_fields = ("last_run_at", "last_snapshot")
