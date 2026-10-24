"""
Counterintelligence and Activity Correlation Data Models for aa_bb.
"""

# Standard Library
import hashlib
import json

# Django
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class CIIdentity(models.Model):
    """
    Time-effective mapping between EVE character, Alliance Auth user,
    Discord snowflake ID, and Mumble username.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_identities"
    )
    character_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    discord_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    mumble_username = models.CharField(
        max_length=255, null=True, blank=True, db_index=True
    )
    effective_from = models.DateTimeField(default=timezone.now, db_index=True)
    effective_until = models.DateTimeField(null=True, blank=True, db_index=True)
    is_verified = models.BooleanField(default=False)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Identity")
        verbose_name_plural = _("CI Identities")
        indexes = [
            models.Index(fields=["user", "effective_from", "effective_until"]),
            models.Index(fields=["character_id", "effective_from"]),
            models.Index(fields=["discord_id", "effective_from"]),
            models.Index(fields=["mumble_username", "effective_from"]),
        ]

    def __str__(self):
        parts = [f"User:{self.user.username}"]
        if self.character_id:
            parts.append(f"Char:{self.character_id}")
        if self.discord_id:
            parts.append(f"Discord:{self.discord_id}")
        if self.mumble_username:
            parts.append(f"Mumble:{self.mumble_username}")
        return f"CIIdentity({', '.join(parts)})"

    def is_active_at(self, dt=None):
        if dt is None:
            dt = timezone.now()
        if self.effective_from and self.effective_from > dt:
            return False
        if self.effective_until and self.effective_until <= dt:
            return False
        return True


class CIOperation(models.Model):
    """
    Operational event window representing a fleet or strategic activity.
    """

    name = models.CharField(max_length=255)
    operation_type = models.CharField(max_length=64, default="strategic")
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(null=True, blank=True, db_index=True)
    fleet_commander = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ci_commanded_operations",
    )
    secrecy_level = models.CharField(max_length=32, default="standard")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Operation")
        verbose_name_plural = _("CI Operations")
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=["start_time", "end_time"]),
            models.Index(fields=["operation_type"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.start_time:%Y-%m-%d %H:%M})"


class CIOperationObjective(models.Model):
    """
    Specific objective or context associated with an operation (e.g. Recon, Logi, Briefing).
    """

    operation = models.ForeignKey(
        CIOperation, on_delete=models.CASCADE, related_name="objectives"
    )
    name = models.CharField(max_length=255)
    objective_type = models.CharField(
        max_length=64
    )  # strategic_defense, capital, qrf, recon, logi, combat, briefing
    expected_roles = models.JSONField(default=list, blank=True)
    disclosure_time = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Operation Objective")
        verbose_name_plural = _("CI Operation Objectives")

    def __str__(self):
        return f"{self.operation.name} - {self.name} ({self.objective_type})"


class CIOperationParticipant(models.Model):
    """
    Authoritative fleet participation credited strictly by FC submission.
    """

    operation = models.ForeignKey(
        CIOperation, on_delete=models.CASCADE, related_name="participants"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_participations"
    )
    character_id = models.BigIntegerField(db_index=True)
    confirmed_by_fc = models.BooleanField(default=True)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Operation Participant")
        verbose_name_plural = _("CI Operation Participants")
        unique_together = ("operation", "character_id")
        indexes = [
            models.Index(fields=["operation", "user"]),
            models.Index(fields=["character_id"]),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.character_id}) in {self.operation.name}"


class CIEvent(models.Model):
    """
    Normalized counterintel event with immutable source data and deduplication hash.
    """

    source = models.CharField(
        max_length=64, db_index=True
    )  # mumble, discord, zkill, pap, ping
    source_event_id = models.CharField(max_length=255, db_index=True)
    timestamp = models.DateTimeField(db_index=True)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ci_events",
    )
    operation = models.ForeignKey(
        CIOperation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )
    payload = models.JSONField(default=dict)
    dedup_hash = models.CharField(max_length=64, unique=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Event")
        verbose_name_plural = _("CI Events")
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["source", "timestamp"]),
            models.Index(fields=["user", "timestamp"]),
            models.Index(fields=["operation", "timestamp"]),
        ]

    def __str__(self):
        return f"[{self.source}] {self.source_event_id} @ {self.timestamp:%Y-%m-%d %H:%M:%S}"

    @staticmethod
    def generate_dedup_hash(
        source: str, source_event_id: str, timestamp, payload: dict = None
    ) -> str:
        ts_str = timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp)
        payload_str = json.dumps(payload or {}, sort_keys=True)
        raw = f"{source}:{source_event_id}:{ts_str}:{payload_str}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CISession(models.Model):
    """
    Contiguous communication session (Mumble connection or Discord voice presence).
    """

    source = models.CharField(max_length=64)  # mumble, discord
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_sessions"
    )
    start_time = models.DateTimeField(db_index=True)
    end_time = models.DateTimeField(null=True, blank=True, db_index=True)
    channel_name = models.CharField(max_length=255, blank=True)
    reconnect_count = models.PositiveIntegerField(default=0)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Session")
        verbose_name_plural = _("CI Sessions")
        ordering = ["-start_time"]
        indexes = [
            models.Index(fields=["user", "start_time"]),
            models.Index(fields=["source", "start_time"]),
        ]

    def __str__(self):
        end = self.end_time.strftime("%H:%M:%S") if self.end_time else "ongoing"
        return f"{self.user.username} {self.source} [{self.start_time:%Y-%m-%d %H:%M:%S} - {end}]"

    @property
    def duration_seconds(self) -> float:
        end = self.end_time or timezone.now()
        return max(0.0, (end - self.start_time).total_seconds())


class CICorrelation(models.Model):
    """
    Versioned correlation analysis finding between an operation, user, and observed anomalies.
    """

    operation = models.ForeignKey(
        CIOperation, on_delete=models.CASCADE, related_name="correlations"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_correlations"
    )
    anomaly_type = models.CharField(max_length=64)
    confidence = models.FloatField(default=0.0)
    data_completeness = models.FloatField(default=1.0)
    algorithm_version = models.CharField(max_length=32)
    source_event_ids = models.JSONField(default=list)
    explanation = models.TextField()
    computed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Correlation")
        verbose_name_plural = _("CI Correlations")
        ordering = ["-computed_at"]
        indexes = [
            models.Index(fields=["operation", "user"]),
            models.Index(fields=["anomaly_type", "confidence"]),
        ]

    def __str__(self):
        return f"Correlation({self.user.username}, {self.operation.name}, {self.anomaly_type}, conf={self.confidence:.2f})"


class CIInvestigation(models.Model):
    """
    Counterintelligence investigation case under human review.
    """

    STATUS_CHOICES = [
        ("pending", _("Needs Review")),
        ("in_review", _("Under Investigation")),
        ("cleared", _("Cleared / Explained")),
        ("escalated", _("Escalated")),
        ("dismissed", _("Dismissed")),
    ]

    operation = models.ForeignKey(
        CIOperation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="investigations",
    )
    subject_user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_investigations"
    )
    status = models.CharField(
        max_length=32, choices=STATUS_CHOICES, default="pending", db_index=True
    )
    assigned_reviewer = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_ci_cases",
    )
    agent_summary = models.TextField(blank=True)
    reviewer_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Investigation")
        verbose_name_plural = _("CI Investigations")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["subject_user", "status"]),
        ]

    def __str__(self):
        op_name = self.operation.name if self.operation else "General"
        return f"Investigation({self.subject_user.username} @ {op_name} - {self.get_status_display()})"


class CIOverride(models.Model):
    """
    Documented exemption or disposition override for specific anomaly types.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="ci_overrides"
    )
    reason = models.TextField()
    exempt_anomaly_types = models.JSONField(default=list)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_ci_overrides",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "aa_bb"
        verbose_name = _("CI Override")
        verbose_name_plural = _("CI Overrides")

    def __str__(self):
        return f"CIOverride({self.user.username}: {self.exempt_anomaly_types})"
