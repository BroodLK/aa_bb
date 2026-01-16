"""
Admin registrations for every BigBrother-related model.

Most models are singletons that gate optional modules. The helpers below
ensure their admin entries only appear when the relevant feature is enabled
and prevent accidental multi-row creation of what should be one-off configs.
"""

from solo.admin import SingletonModelAdmin

from django.contrib import admin
from .app_settings import afat_active, discordbot_active, charlink_active
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
    ComplianceThread,
    EveItemPrice,
)

@admin.register(BigBrotherConfig)
class BB_ConfigAdmin(SingletonModelAdmin):
    fieldsets = (
        (
            "Core Settings",
            {
                "fields": (
                    "is_active",
                    "limit_to_main_corp",
                )
            },
        ),
        (
            "Notification Toggles",
            {
                "description": "Enable/disable specific notification types sent to Discord",
                "fields": (
                    "ct_notify",
                    "awox_notify",
                    "cyno_notify",
                    "sp_inject_notify",
                    "clone_notify",
                    "clone_state_notify",
                    "asset_notify",
                    "contact_notify",
                    "exclude_neutral_contacts",
                    "contract_notify",
                    "mail_notify",
                    "transaction_notify",
                    "show_market_transactions",
                    "new_user_notify",
                    "ticket_notify_man",
                    "ticket_notify_auto",
                ),
            },
        ),
        (
            "Market Transaction Settings - Toggles",
            {
                "classes": ("market-transaction-settings-fieldset",),
                "description": "Market transaction monitoring options",
                "fields": (
                    "market_transactions_show_major_hubs",
                    "market_transactions_show_secondary_hubs",
                    "market_transactions_threshold_alert",
                    "market_transactions_price_instant",
                )
            },
        ),
        (
            "Market Transaction Settings - Configuration",
            {
                "classes": ("market-transaction-settings-fieldset",),
                "description": "Market price and API configuration",
                "fields": (
                    "market_transactions_excluded_systems",
                    "market_transactions_threshold_percent",
                    "market_transactions_price_method",
                    "market_transactions_janice_api_key",
                    "market_transactions_fuzzwork_station_id",
                    "market_transactions_price_max_age",
                )
            },
        ),
        (
            "Hostile & Whitelist - Entity Lists",
            {
                "description": "Comma-separated lists of entity IDs",
                "fields": (
                    "hostile_alliances",
                    "hostile_corporations",
                    "whitelist_alliances",
                    "whitelist_corporations",
                    "ignored_corporations",
                    "member_corporations",
                    "member_alliances",
                    "excluded_systems",
                    "excluded_stations",
                    "custom_hauling_corps",
                    "alliance_blacklist_url",
                    "external_blacklist_url",
                )
            },
        ),
        (
            "Hostile & Whitelist - Rules",
            {
                "description": "Define hostile detection rules and exclusions",
                "fields": (
                    "hostile_everyone_else",
                    "consider_nullsec_hostile",
                    "consider_lowsec_hostile",
                    "consider_all_structures_hostile",
                    "consider_npc_stations_hostile",
                    "exclude_high_sec",
                    "exclude_low_sec",
                    "hostile_assets_ships_only",
                    "exclude_hauling_corps_from_courier",
                    # aa-contacts import (conditionally add fields)
                    *(
                        (
                            "auto_import_contacts_enabled",
                            "contacts_source_alliances",
                            "contacts_source_corporations",
                            "contacts_handle_neutrals",
                        )
                        if AA_CONTACTS_INSTALLED
                        else ()
                    ),
                )
            },
        ),
        (
            "User States & Membership",
            {
                "description": "Define which states and corps/alliances are monitored",
                "fields": (
                    "bb_guest_states",
                    "bb_member_states",
                    "hide_unaudited_users",
                )
            },
        ),
        (
            "Discord Webhooks",
            {
                "description": "Configure Discord webhook URLs for various notification types",
                "fields": (
                    "webhook",
                    "user_compliance_webhook",
                    "corp_compliance_webhook",
                    "loawebhook",
                    "stats_webhook",
                    "dailywebhook",
                    "optwebhook1",
                    "optwebhook2",
                    "optwebhook3",
                    "optwebhook4",
                    "optwebhook5",
                )
            },
        ),
        (
            "Ping Roles & Message Types",
            {
                "description": "Map message categories to Discord role pings",
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
            "Feature Activation",
            {
                "description": "Enable or disable specific features",
                "fields": (
                    "is_warmer_active",
                    "discord_message_tracking",
                    "is_loa_active",
                    "is_paps_active",
                    "are_recurring_stats_active",
                    "are_daily_messages_active",
                    "are_opt_messages1_active",
                    "are_opt_messages2_active",
                    "are_opt_messages3_active",
                    "are_opt_messages4_active",
                    "are_opt_messages5_active",
                ),
            },
        ),
        (
            "Schedules & Timing",
            {
                "description": "Configure when features run",
                "fields": (
                    "loa_max_logoff_days",
                    "stats_schedule",
                    "dailyschedule",
                    "optschedule1",
                    "optschedule2",
                    "optschedule3",
                    "optschedule4",
                    "optschedule5",
                ),
            },
        ),
        (
            "Performance & Update Settings - Toggles",
            {
                "description": "Control update behavior",
                "fields": (
                    "clone_state_always_recheck",
                    "update_backlog_notify",
                ),
            },
        ),
        (
            "Performance & Update Settings - Configuration",
            {
                "description": "Timing and threshold configuration",
                "fields": (
                    "update_stagger_seconds",
                    "update_cache_ttl_hours",
                    "update_maintenance_window_start",
                    "update_maintenance_window_end",
                    "update_backlog_threshold",
                ),
            },
        ),
        (
            "Required ESI Scopes",
            {
                "classes": ("collapse",),
                "description": "ESI scopes required for character and corporation data access",
                "fields": (
                    "character_scopes",
                    "corporation_scopes",
                ),
            },
        ),
        (
            "Main Corp / Alliance Info",
            {
                "classes": ("collapse",),
                "description": "Auto-populated information about your main corporation and alliance",
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
        "update_last_dispatch_count",
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
            ("contacts_source_alliances", "contacts_source_corporations")
            if AA_CONTACTS_INSTALLED
            else ()
        ),
    )

    class Media:
        js = ("aa_bb/js/admin_bb_config.js",)

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
    readonly_fields = ("ticket_counter",)
    filter_horizontal = (
        "excluded_users",
    )

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            ("Ticket System Configuration", {
                'description': 'Configure how tickets are created and managed',
                'fields': ('ticket_type', 'role_id', 'hr_forum_webhook', 'Forum_Channel_ID', 'ticket_counter', 'excluded_users')
            }),
        ]

        if discordbot_active():
            fieldsets.insert(1, ('Private Channel Settings (Bot)', {
                'classes': ('private-channel-fieldset',),
                'description': 'Category ID for private ticket channels (only for private_channel ticket type)',
                'fields': ('Category_ID',)
            }))

        # Corp Compliance Check - only show compliance_filter if charlink is installed
        corp_check_fields = []
        if charlink_active():
            corp_check_fields.append('compliance_filter')
        corp_check_fields.extend(['corp_check_enabled', 'corp_check_include_user', 'corp_check', 'corp_check_frequency', 'corp_check_reason', 'corp_check_reminder'])

        fieldsets.append(('Corp Compliance Check', {
            'classes': ('compliance-check-fieldset',),
            'description': 'Verify users meet corp/alliance requirements',
            'fields': tuple(corp_check_fields)
        }))

        fieldsets.extend([
            ('Inactivity Check', {
                'classes': ('compliance-check-fieldset',),
                'description': 'Monitor user activity and last login time',
                'fields': ('afk_check_enabled', 'afk_check_include_user', 'Max_Afk_Days', 'afk_check', 'afk_check_frequency', 'afk_check_reason', 'afk_check_reminder')
            }),
            ('Character Removal Check', {
                'classes': ('compliance-check-fieldset',),
                'description': 'Alert when characters are removed from authentication',
                'fields': ('char_removed_enabled', 'char_removed_include_user', 'char_removed_reason')
            }),
            ('AWOX Kill Monitor', {
                'classes': ('compliance-check-fieldset',),
                'description': 'Track and alert on friendly fire incidents',
                'fields': ('awox_monitor_enabled', 'awox_kill_include_user', 'awox_kill_reason')
            }),
        ])

        if discordbot_active():
            # Insert Discord checks together
            discord_fieldsets = [
                ('Discord Link Check', {
                    'classes': ('compliance-check-fieldset',),
                    'description': 'Verify users have linked their Discord account',
                    'fields': ('discord_check_enabled', 'discord_check', 'discord_check_frequency', 'discord_check_reason', 'discord_check_reminder')
                }),
                ('Discord Inactivity Check', {
                    'classes': ('compliance-check-fieldset',),
                    'description': 'Monitor Discord activity and message participation',
                    'fields': ('discord_inactivity_enabled', 'discord_inactivity_include_user', 'discord_inactivity_days', 'discord_inactivity_reason')
                }),
            ]
            # Find position after Inactivity Check
            idx = 0
            for i, (name, _) in enumerate(fieldsets):
                if 'Inactivity Check' in name:
                    idx = i + 1
                    break
            for fs in reversed(discord_fieldsets):
                fieldsets.insert(idx, fs)

        if afat_active():
            fieldsets.append(('PAP Compliance Check', {
                'classes': ('compliance-check-fieldset',),
                'description': 'Monitor fleet participation (PAPs) requirements',
                'fields': ('paps_check_enabled', 'paps_check_include_user', 'max_months_without_pap_compliance', 'starting_pap_compliance', 'paps_check', 'paps_check_frequency', 'paps_check_reason', 'paps_check_reminder')
            }))

        return fieldsets

    class Media:
        js = ("aa_bb/js/admin_ticket_config.js",)

    def get_form(self, request, obj=None, **kwargs):
        from django import forms
        form = super().get_form(request, obj, **kwargs)

        # Make role_id a textarea
        if 'role_id' in form.base_fields:
            form.base_fields['role_id'].widget = forms.Textarea(attrs={'rows': 3, 'cols': 40})

        if not discordbot_active():
            from .models import TicketToolConfig
            # Restrict choices if bot is not active
            allowed_choices = [
                (TicketToolConfig.TICKET_TYPE_FORUM_THREAD, 'Public Forum Threads (Webhook)'),
                (TicketToolConfig.TICKET_TYPE_AUTH_ONLY, 'Auth Only (No Discord)'),
            ]
            form.base_fields['ticket_type'].choices = allowed_choices
        return form

    def has_add_permission(self, request):
        """Prevent duplicate ticket config entries."""
        if TicketToolConfig.objects.exists():  # Ticket config should remain singleton.
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        """Allow deletes when operators need to reset settings."""
        return True




@admin.register(EveItemPrice)
class EveItemPriceAdmin(admin.ModelAdmin):
    list_display = ("eve_type_id", "buy", "sell", "updated")
    search_fields = ("eve_type_id",)


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


class ReasonFilter(admin.SimpleListFilter):
    title = 'reason'
    parameter_name = 'reason'

    def lookups(self, request, model_admin):
        from .models import ComplianceTicket
        reasons = list(ComplianceTicket.REASONS)
        if not afat_active():
            reasons = [r for r in reasons if r[0] != "paps_check"]
        if not discordbot_active():
            reasons = [r for r in reasons if r[0] != "discord_check"]
        return reasons

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(reason=self.value())
        return queryset


@admin.register(ComplianceTicket)
class ComplianceTicketConfig(admin.ModelAdmin):
    """History of tickets issued by the automation layer."""
    list_display = ["user", "ticket_id", "reason", "is_resolved", "is_exception"]
    list_filter = ["is_resolved", "is_exception", ReasonFilter]
    readonly_fields = ["created_at"]

    actions = ["mark_as_exception", "clear_exception", "mark_as_resolved", "mark_as_open"]

    def mark_as_exception(self, request, queryset):
        """Mark selected tickets as exceptions."""
        count = queryset.update(is_exception=True, exception_reason=f"Marked as exception by {request.user.username}")
        self.message_user(request, f"{count} ticket(s) marked as exception.")
    mark_as_exception.short_description = "Mark selected tickets as exception"

    def clear_exception(self, request, queryset):
        """Clear exception status from selected tickets."""
        count = queryset.update(is_exception=False, exception_reason=None)
        self.message_user(request, f"{count} ticket(s) exception status cleared.")
    clear_exception.short_description = "Clear exception status"

    def mark_as_resolved(self, request, queryset):
        """Mark selected tickets as resolved."""
        count = queryset.update(is_resolved=True)
        self.message_user(request, f"{count} ticket(s) marked as resolved.")
    mark_as_resolved.short_description = "Mark selected tickets as resolved"

    def mark_as_open(self, request, queryset):
        """Mark selected tickets as open."""
        count = queryset.update(is_resolved=False, is_exception=False)
        self.message_user(request, f"{count} ticket(s) marked as open.")
    mark_as_open.short_description = "Mark selected tickets as open"

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not afat_active():
            qs = qs.exclude(reason="paps_check")
        if not discordbot_active():
            qs = qs.exclude(reason="discord_check")
        return qs


@admin.register(ComplianceThread)
class ComplianceThreadAdmin(admin.ModelAdmin):
    """Mapping of user/reason to Discord thread IDs."""
    list_display = ["user", "reason", "thread_id"]
    list_filter = [ReasonFilter]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if not afat_active():
            qs = qs.exclude(reason="paps_check")
        if not discordbot_active():
            qs = qs.exclude(reason="discord_check")
        return qs


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
_MARKET_OBJECT_NAMES = {"EveItemPrice", "ProcessedTransaction", "SusTransactionNote"}
_ORIG_GET_APP_LIST = admin.site.get_app_list


def _filtered_get_app_list(request, app_label=None):
    app_list = _ORIG_GET_APP_LIST(request, app_label)

    is_afat = afat_active()
    config = BigBrotherConfig.get_solo()
    show_market = getattr(config, "show_market_transactions", False)

    filtered = []
    for app in app_list:
        label = app.get("app_label")

        # Exclude AFAT's own admin section if present.
        if not is_afat and label == "afat":
            continue

        # Filter models within our app
        if label == "aa_bb":
            models = app.get("models", [])
            if not is_afat:
                models = [
                    m for m in models if m.get("object_name") not in _PAP_OBJECT_NAMES
                ]
            if not show_market:
                models = [
                    m for m in models if m.get("object_name") not in _MARKET_OBJECT_NAMES
                ]
            app = {**app, "models": models}

        # Drop empty app groups
        if app.get("models"):
            filtered.append(app)

    return filtered


admin.site.get_app_list = _filtered_get_app_list
