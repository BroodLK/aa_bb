"""
Django Admin integration for counterintelligence models.
"""

# Django
from django.contrib import admin

from .models import (
    CICorrelation,
    CIEvent,
    CIIdentity,
    CIInvestigation,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CIOverride,
    CISession,
)


@admin.register(CIOperation)
class CIOperationAdmin(admin.ModelAdmin):
    list_display = ("name", "operation_type", "secrecy_level", "start_time", "end_time", "fleet_commander")
    list_filter = ("operation_type", "secrecy_level", "start_time")
    search_fields = ("name", "notes", "fleet_commander__username")


@admin.register(CIOperationObjective)
class CIOperationObjectiveAdmin(admin.ModelAdmin):
    list_display = ("operation", "name", "objective_type", "disclosure_time")
    list_filter = ("objective_type",)
    search_fields = ("name", "operation__name")


@admin.register(CIOperationParticipant)
class CIOperationParticipantAdmin(admin.ModelAdmin):
    list_display = ("operation", "user", "character_id", "confirmed_by_fc", "recorded_at")
    list_filter = ("confirmed_by_fc", "recorded_at")
    search_fields = ("user__username", "character_id", "operation__name")


@admin.register(CIIdentity)
class CIIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "character_id", "discord_id", "mumble_username", "effective_from", "effective_until", "is_verified")
    list_filter = ("is_verified", "effective_from")
    search_fields = ("user__username", "mumble_username", "character_id", "discord_id")


@admin.register(CIEvent)
class CIEventAdmin(admin.ModelAdmin):
    list_display = ("source", "source_event_id", "timestamp", "user", "operation")
    list_filter = ("source", "timestamp")
    search_fields = ("source_event_id", "user__username", "operation__name")


@admin.register(CISession)
class CISessionAdmin(admin.ModelAdmin):
    list_display = ("source", "user", "start_time", "end_time", "channel_name", "reconnect_count")
    list_filter = ("source", "start_time")
    search_fields = ("user__username", "channel_name")


@admin.register(CICorrelation)
class CICorrelationAdmin(admin.ModelAdmin):
    list_display = ("operation", "user", "anomaly_type", "confidence", "data_completeness", "algorithm_version", "computed_at")
    list_filter = ("anomaly_type", "algorithm_version", "computed_at")
    search_fields = ("user__username", "operation__name", "explanation")


@admin.register(CIInvestigation)
class CIInvestigationAdmin(admin.ModelAdmin):
    list_display = ("id", "subject_user", "operation", "status", "assigned_reviewer", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("subject_user__username", "operation__name", "reviewer_notes", "agent_summary")


@admin.register(CIOverride)
class CIOverrideAdmin(admin.ModelAdmin):
    list_display = ("user", "reason", "created_by", "created_at")
    search_fields = ("user__username", "reason")
