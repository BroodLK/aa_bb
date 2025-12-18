"""
Admin registrations for every BigBrother-related model.

Most models are singletons that gate optional modules. The helpers below
ensure their admin entries only appear when the relevant feature is enabled
and prevent accidental multi-row creation of what should be one-off configs.
"""

from solo.admin import SingletonModelAdmin

from django.contrib import admin
from .app_settings import afat_active
from django.contrib.admin.sites import NotRegistered

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
    AA_CONTACTS_INSTALLED,
    TicketToolConfig,
    PapCompliance,
    LeaveRequest,
    ComplianceTicket,
    BigBrotherRedditSettings,
    BigBrotherRedditMessage
)

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
                    # aa-contacts import (conditionally add fields)
                    *(
                        (
                            "auto_import_contacts_enabled",
                            "contacts_source_alliances",
                            "contacts_handle_neutrals",
                        )
                        if AA_CONTACTS_INSTALLED
                        else ()
                    ),
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
    )
    filter_horizontal = (
        "pingrole1_messages",
        "pingrole2_messages",
        "here_messages",
        "everyone_messages",
        "bb_guest_states",
        "bb_member_states",
        # aa-contacts M2M (only if installed)
        *(
            ("contacts_source_alliances",)
            if AA_CONTACTS_INSTALLED
            else ()
        ),
    )

    def has_add_permission(self, request):
        """Prevent duplicate singleton rows."""
        if BigBrotherConfig.objects.exists():  # Disallow when a config already exists.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Always allow deleting to keep parity with default behavior."""
        return True


@admin.register(PapsConfig)
class PapsConfigAdmin(SingletonModelAdmin):
    """Controls PAP multipliers/thresholds; singleton per installation."""
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
class TicketToolConfigAdmin(SingletonModelAdmin):
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


@admin.register(BigBrotherRedditSettings)
class BigBrotherRedditSettingsAdmin(SingletonModelAdmin):
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
class BigBrotherRedditMessageAdmin(admin.ModelAdmin):
    """Manage the pool of canned Reddit ads."""
    list_display = ("title", "used_in_cycle", "created")
    list_filter = ("used_in_cycle",)
    search_fields = ("title", "content")


@admin.register(Messages)
class DailyMessageConfig(admin.ModelAdmin):
    """Standard daily webhook messages rotated each cycle."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages1)
class OptMessage1Config(admin.ModelAdmin):
    """Optional webhook stream #1."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages2)
class OptMessage2Config(admin.ModelAdmin):
    """Optional webhook stream #2."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages3)
class OptMessage3Config(admin.ModelAdmin):
    """Optional webhook stream #3."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages4)
class OptMessage4Config(admin.ModelAdmin):
    """Optional webhook stream #4."""
    search_fields = ["text"]
    list_display = ["text", "sent_in_cycle"]


@admin.register(OptMessages5)
class OptMessage5Config(admin.ModelAdmin):
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
class ComplianceTicketConfig(admin.ModelAdmin):
    """History of tickets issued by the automation layer."""
    list_display = ["user", "ticket_id", "reason"]


@admin.register(LeaveRequest)
class LeaveRequestConfig(admin.ModelAdmin):
    """Expose LeaveRequest records to staff when LoA is enabled."""
    list_display = ["main_character", "start_date", "end_date", "reason", "status"]


@admin.register(PapCompliance)
class PapComplianceConfig(admin.ModelAdmin):
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

if not afat_active():
    for _m in (PapsConfig, PapCompliance):
        try:
            admin.site.unregister(_m)
        except NotRegistered:
            pass

_PAP_OBJECT_NAMES = {"PapsConfig", "PapCompliance"}
_ORIG_GET_APP_LIST = admin.site.get_app_list


def _filtered_get_app_list(request, app_label=None):
    app_list = _ORIG_GET_APP_LIST(request, app_label)

    if afat_active():
        return app_list

    filtered = []
    for app in app_list:
        # If AFAT itself has an admin section, drop it.
        if app.get("app_label") == "afat":
            continue

        # PAP models (in this project) show under aa_bb, so remove them there.
        if app.get("app_label") == "aa_bb":
            models = app.get("models", [])
            models = [m for m in models if m.get("object_name") not in _PAP_OBJECT_NAMES]
            app = {**app, "models": models}

        # Drop empty app groups so the menu section disappears entirely.
        if app.get("models"):
            filtered.append(app)

    return filtered


admin.site.get_app_list = _filtered_get_app_list
