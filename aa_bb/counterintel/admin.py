"""
Django Admin integration for counterintelligence models.
"""

# Standard Library
import logging

# Third Party
from solo.admin import SingletonModelAdmin

# Django
from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from .models import (
    CICorrelation,
    CIEvent,
    CIIdentity,
    CIInvestigation,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CIOverride,
    CIScoringConfig,
    CISession,
)

logger = logging.getLogger(__name__)


@admin.register(CIScoringConfig)
class CIScoringConfigAdmin(SingletonModelAdmin):
    fieldsets = (
        (
            _("Risk score weights"),
            {
                "description": _(
                    "How much each factor counts towards a member's risk score. "
                    "They must add up to 1.00 (100%)."
                ),
                "fields": (
                    "weight_comms",
                    "weight_discord",
                    "weight_alt",
                    "weight_deviation",
                    "weight_bb_signals",
                    "weight_leak",
                    "weight_accountability",
                ),
            },
        ),
        (
            _("Finding confidence"),
            {
                "description": _(
                    "Confidence (0–1) each finding is reported with. At or above the "
                    "case-opening confidence a case opens; at or above the alert "
                    "confidence Discord is alerted."
                ),
                "fields": (
                    "conf_pre_contact_habitual",
                    "conf_pre_contact_outlier",
                    "conf_pre_contact_no_history",
                    "conf_briefing",
                    "conf_combat_absence",
                    "conf_alt_proximity",
                    "conf_alt_hostile",
                    "conf_shared_mumble",
                    "conf_disconnect_ring",
                    "conf_uncredited_alt",
                    "conf_eve_logoff",
                    "conf_unaudited",
                ),
            },
        ),
        (
            _("Calculated confidence bounds"),
            {
                "description": _(
                    "Findings whose confidence is calculated from the evidence."
                ),
                "fields": (
                    "comms_base",
                    "comms_max",
                    "comms_no_channel_cap",
                    "bb_sensitive_base",
                    "bb_sensitive_scale",
                    "leak_base",
                    "leak_max",
                ),
            },
        ),
        (
            _("Alerts and cases"),
            {"fields": ("alert_confidence", "alert_risk", "auto_triage_confidence")},
        ),
        (
            _("Leak cross-check"),
            {"fields": ("leak_min_ops", "leak_min_rate", "leak_min_lift")},
        ),
        (
            _("Secrecy multipliers"),
            {
                "description": _(
                    "Risk multiplier per secrecy level; Standard is always 1.0. "
                    "Must not decrease from Restricted to Top Secret."
                ),
                "fields": (
                    "secrecy_restricted",
                    "secrecy_classified",
                    "secrecy_top_secret",
                ),
            },
        ),
        (
            _("Big Brother flag severities"),
            {
                "description": _(
                    "How strongly each Big Brother flag counts (0–1). Several flags "
                    "add up."
                ),
                "fields": (
                    "sev_alliance_blacklist",
                    "sev_coalition_blacklist",
                    "sev_awox",
                    "sev_hostile_clones",
                    "sev_sus_contacts",
                    "sev_sus_contracts",
                    "sev_sus_mails",
                    "sev_hostile_assets",
                    "sev_sus_trans",
                ),
            },
        ),
        (_("Case dispositions"), {"fields": ("override_days",)}),
        (_("Privacy"), {"fields": ("hide_discord_channel_names",)}),
    )

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser or request.user.has_perm("aa_bb.can_manage_ci")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        messages.info(
            request,
            _(
                "Recent operations are being re-scored in the background; findings "
                "update as each operation finishes."
            ),
        )


@admin.register(CIOperation)
class CIOperationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "operation_type",
        "secrecy_level",
        "start_time",
        "end_time",
        "fleet_commander",
    )
    list_filter = ("operation_type", "secrecy_level", "start_time")
    search_fields = ("name", "notes", "fleet_commander__username")
    actions = ("rescore_selected",)

    @admin.action(
        description=_("Re-score selected operations with the current scoring settings"),
        permissions=("rescore",),
    )
    def rescore_selected(self, request, queryset):
        # AA BigBrother
        from aa_bb.counterintel.services.correlation import CorrelationEngine

        rescored, failed = 0, []
        for operation in queryset:
            try:
                CorrelationEngine.correlate_operation(operation)
            except Exception:
                logger.exception(f"Re-scoring failed for operation {operation.id}")
                failed.append(operation.name)
                continue
            rescored += 1
        if rescored:
            self.message_user(
                request,
                _("Re-scored %(count)d operation(s).") % {"count": rescored},
                messages.SUCCESS,
            )
        if failed:
            self.message_user(
                request,
                _("Re-scoring failed for: %(names)s. See the server log.")
                % {"names": ", ".join(failed)},
                messages.ERROR,
            )

    def has_rescore_permission(self, request):
        return request.user.is_superuser or request.user.has_perm("aa_bb.can_manage_ci")


@admin.register(CIOperationObjective)
class CIOperationObjectiveAdmin(admin.ModelAdmin):
    list_display = ("operation", "name", "objective_type", "disclosure_time")
    list_filter = ("objective_type",)
    search_fields = ("name", "operation__name")


@admin.register(CIOperationParticipant)
class CIOperationParticipantAdmin(admin.ModelAdmin):
    list_display = (
        "operation",
        "user",
        "character_id",
        "confirmed_by_fc",
        "recorded_at",
    )
    list_filter = ("confirmed_by_fc", "recorded_at")
    search_fields = ("user__username", "character_id", "operation__name")


@admin.register(CIIdentity)
class CIIdentityAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "character_id",
        "discord_id",
        "mumble_username",
        "effective_from",
        "effective_until",
        "is_verified",
    )
    list_filter = ("is_verified", "effective_from")
    search_fields = ("user__username", "mumble_username", "character_id", "discord_id")


@admin.register(CIEvent)
class CIEventAdmin(admin.ModelAdmin):
    list_display = ("source", "source_event_id", "timestamp", "user", "operation")
    list_filter = ("source", "timestamp")
    search_fields = ("source_event_id", "user__username", "operation__name")


@admin.register(CISession)
class CISessionAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "user",
        "start_time",
        "end_time",
        "channel_name",
        "reconnect_count",
    )
    list_filter = ("source", "start_time")
    search_fields = ("user__username", "channel_name")


@admin.register(CICorrelation)
class CICorrelationAdmin(admin.ModelAdmin):
    list_display = (
        "operation",
        "user",
        "anomaly_type",
        "confidence",
        "data_completeness",
        "algorithm_version",
        "computed_at",
    )
    list_filter = ("anomaly_type", "algorithm_version", "computed_at")
    search_fields = ("user__username", "operation__name", "explanation")


@admin.register(CIInvestigation)
class CIInvestigationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "subject_user",
        "operation",
        "status",
        "assigned_reviewer",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = (
        "subject_user__username",
        "operation__name",
        "reviewer_notes",
        "agent_summary",
    )


@admin.register(CIOverride)
class CIOverrideAdmin(admin.ModelAdmin):
    list_display = ("user", "reason", "created_by", "created_at")
    search_fields = ("user__username", "reason")
