"""
Multi-dimensional counterintelligence correlation and behavioral anomaly engine.
"""

# Standard Library
from datetime import timedelta
import logging

# Django
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import (
    CICorrelation,
    CIEvent,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CIOverride,
    CISession,
)
from .baseline import BaselineEngine
from .operations import CIOperationService

logger = logging.getLogger(__name__)


class CorrelationEngine:
    """
    Evaluates multi-source activity against operational windows, personal baselines,
    and role contexts to generate versioned, reproducible correlation findings.
    """

    ALGORITHM_VERSION = "1.0.0"

    @classmethod
    def correlate_operation(
        cls,
        operation: CIOperation,
        algorithm_version: str = ALGORITHM_VERSION,
    ) -> list[CICorrelation]:
        """
        Execute full correlation analysis across all dimensions for an operation.
        """
        timeline = CIOperationService.reconstruct_timeline(operation)
        findings = []

        confirmed_user_ids = {p["user_id"] for p in timeline["participants"]}
        op_start = operation.start_time
        op_end = operation.end_time or (op_start + timedelta(hours=2))

        # 1. Comms presence without PAP credit
        findings.extend(cls._detect_comms_without_pap(operation, timeline, confirmed_user_ids, algorithm_version))

        # 2. Pre-contact disconnect detection
        findings.extend(cls._detect_pre_contact_disconnects(operation, timeline, algorithm_version))

        # 3. Briefing attendance without deployment
        findings.extend(cls._detect_briefing_anomalies(operation, timeline, algorithm_version))

        # 4. Role-aware combat participation check
        findings.extend(cls._detect_combat_discrepancies(operation, timeline, confirmed_user_ids, algorithm_version))

        # Filter out findings suppressed by documented CIOverrides
        persisted_findings = []
        user_overrides_map = {}
        for finding in findings:
            user = finding["user"]
            anomaly_type = finding["anomaly_type"]

            if user.id not in user_overrides_map:
                user_overrides_map[user.id] = list(CIOverride.objects.filter(user=user))

            is_exempt = any(
                anomaly_type in (ovr.exempt_anomaly_types or [])
                for ovr in user_overrides_map[user.id]
            )

            if is_exempt:
                logger.info(f"Anomaly {anomaly_type} for user {user.username} suppressed by active CIOverride.")
                continue

            corr, _ = CICorrelation.objects.update_or_create(
                operation=operation,
                user=user,
                anomaly_type=anomaly_type,
                defaults={
                    "confidence": finding["confidence"],
                    "data_completeness": finding["data_completeness"],
                    "algorithm_version": algorithm_version,
                    "source_event_ids": finding["source_event_ids"],
                    "explanation": finding["explanation"],
                },
            )
            persisted_findings.append(corr)

        return persisted_findings

    @classmethod
    def _detect_comms_without_pap(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        user_sessions = {}
        for s in timeline["sessions"]:
            uid = s["user_id"]
            user_sessions.setdefault(uid, []).append(s)

        for uid, sessions in user_sessions.items():
            if uid not in confirmed_user_ids:
                total_duration = sum(s["duration_seconds"] for s in sessions)
                # Significant presence threshold: > 5 minutes
                if total_duration >= 300:
                    user = User.objects.filter(id=uid).first()
                    if not user:
                        continue
                    confidence = min(0.95, 0.4 + (total_duration / 3600.0) * 0.5)
                    source_ids = [f"session:{s['id']}" for s in sessions]
                    channels = ", ".join(filter(None, {s["channel_name"] for s in sessions})) or "General Comms"
                    explanation = (
                        f"User {user.username} observed in operational comms ({channels}) for "
                        f"{int(total_duration // 60)} minutes without confirmed FC fleet credit."
                    )
                    findings.append({
                        "user": user,
                        "anomaly_type": "comms_without_pap",
                        "confidence": round(confidence, 2),
                        "data_completeness": 0.95,
                        "source_event_ids": source_ids,
                        "explanation": explanation,
                    })

        return findings

    @classmethod
    def _detect_pre_contact_disconnects(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        combat_events = [e for e in timeline["events"] if e["source"] == "zkill"]
        if not combat_events:
            return findings

        # First combat contact timestamp
        first_contact = min(e["timestamp"] for e in combat_events)
        critical_window_start = first_contact - timedelta(minutes=15)

        for s in timeline["sessions"]:
            end_time = s["end_time"]
            if end_time and critical_window_start <= end_time <= first_contact:
                user = User.objects.filter(id=s["user_id"]).first()
                if not user:
                    continue

                minutes_before = int((first_contact - end_time).total_seconds() // 60)
                is_typical, habit_prob = BaselineEngine.is_typical_disconnect_time(user, end_time)

                # Calibrate confidence based on historical habit
                if is_typical:
                    confidence = 0.25  # Low confidence: habit explains logoff
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact, "
                        f"matching their habitual daily logoff schedule ({int(habit_prob*100)}% historical frequency)."
                    )
                else:
                    confidence = 0.85  # High confidence: statistical outlier
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact. "
                        f"This logoff is an outlier relative to their historical activity baseline."
                    )

                findings.append({
                    "user": user,
                    "anomaly_type": "pre_contact_disconnect",
                    "confidence": round(confidence, 2),
                    "data_completeness": 0.90,
                    "source_event_ids": [f"session:{s['id']}", f"zkill:{combat_events[0]['id']}"],
                    "explanation": explanation,
                })

        return findings

    @classmethod
    def _detect_briefing_anomalies(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        briefing_objectives = [o for o in timeline["objectives"] if o["objective_type"] == "briefing"]
        if not briefing_objectives and operation.secrecy_level != "classified":
            return findings

        # Check for users present during briefing who never deployed to subsequent operations
        for s in timeline["sessions"]:
            user = User.objects.filter(id=s["user_id"]).first()
            if not user:
                continue

            recent_fleets = CIOperationParticipant.objects.filter(
                user=user,
                recorded_at__gte=operation.start_time,
                recorded_at__lte=operation.start_time + timedelta(days=7),
            ).count()

            if recent_fleets == 0 and s["duration_seconds"] >= 600:
                findings.append({
                    "user": user,
                    "anomaly_type": "briefing_without_deployment",
                    "confidence": 0.70,
                    "data_completeness": 0.85,
                    "source_event_ids": [f"session:{s['id']}"],
                    "explanation": (
                        f"User {user.username} attended sensitive strategic briefing ({operation.name}) "
                        f"without subsequent fleet deployment over the following 7 days."
                    ),
                })

        return findings

    @classmethod
    def _detect_combat_discrepancies(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        combat_events = [e for e in timeline["events"] if e["source"] == "zkill"]
        if not combat_events or len(combat_events) < 3:
            return findings

        # Check expected roles across objectives
        all_expected_roles = []
        for obj in timeline["objectives"]:
            all_expected_roles.extend(obj.get("expected_roles", []))

        # If operation objectives indicate support/recon/logi, suppress combat absence
        if BaselineEngine.is_role_exempt_from_combat(all_expected_roles):
            logger.info(f"Operation {operation.name} marked with support/exempt roles; suppressing combat absence check.")
            return findings

        combat_user_ids = {e["user_id"] for e in combat_events if e["user_id"]}
        missing_combat_users = confirmed_user_ids - combat_user_ids

        for uid in missing_combat_users:
            user = User.objects.filter(id=uid).first()
            if not user:
                continue

            findings.append({
                "user": user,
                "anomaly_type": "combat_absence_unexplained",
                "confidence": 0.45,
                "data_completeness": 0.80,
                "source_event_ids": [f"op:{operation.id}"],
                "explanation": (
                    f"User {user.username} was confirmed on combat fleet roster but did not appear on "
                    f"any of the {len(combat_events)} recorded killmails for this engagement."
                ),
            })

        return findings
