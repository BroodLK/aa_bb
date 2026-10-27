"""
Read-only AI Investigation Assistant service for counterintelligence triage and case synthesis.
"""

# Standard Library
import logging
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import (
    CICorrelation,
    CIInvestigation,
    CIOperation,
    CIOverride,
    CISession,
)
from .baseline import BaselineEngine
from .correlation import CorrelationEngine

logger = logging.getLogger(__name__)


class CIAgentService:
    """
    Read-only assistant that synthesizes evidence timelines, identifies alternative
    explanations, evaluates data completeness, and drafts findings for human review.
    Zero automated punitive actions.
    """

    @classmethod
    def create_or_update_investigation(
        cls,
        subject_user: User,
        operation: CIOperation = None,
        auto_synthesize: bool = True,
    ) -> CIInvestigation:
        """
        Create or get an investigation case for a subject and operation.
        """
        investigation, created = CIInvestigation.objects.get_or_create(
            subject_user=subject_user,
            operation=operation,
            defaults={
                "status": "pending",
            },
        )

        if auto_synthesize:
            cls.generate_case_findings(investigation)

        return investigation

    @classmethod
    def get_benign_explanations(cls, investigation: CIInvestigation) -> list[dict]:
        """
        Evaluate and extract structured plausible benign hypotheses and confounding factors
        to prevent cognitive fixation and false accusations.
        """
        user = investigation.subject_user
        operation = investigation.operation
        explanations = []

        # 1. Operational Support Role Context
        if operation:
            support_objectives = []
            for obj in operation.objectives.all():
                if BaselineEngine.is_role_exempt_from_combat(obj.expected_roles):
                    support_objectives.append(f"{obj.name} ({obj.objective_type})")
            if support_objectives:
                explanations.append(
                    {
                        "category": "Operational Role Context",
                        "badge": "Support Role",
                        "badge_class": "bg-info text-dark",
                        "text": f"Operation includes registered non-combat support roles ({', '.join(support_objectives)}). Absence from combat killmails is standard operational behavior.",
                    }
                )

        # 2. Network Drops / Connection Volatility
        user_sessions = CISession.objects.filter(user=user).order_by("-start_time")[:10]
        reconnect_total = sum(s.reconnect_count for s in user_sessions)
        if reconnect_total > 2:
            explanations.append(
                {
                    "category": "Connection Volatility",
                    "badge": "Network Instability",
                    "badge_class": "bg-warning text-dark",
                    "text": f"Observed {reconnect_total} recent reconnect events across sessions, consistent with ISP drops or network jitter rather than intentional disengagement.",
                }
            )

        # 3. Logoff Habit vs. Fleet Timing
        baseline = BaselineEngine.calculate_personal_baseline(user)
        if baseline.get("total_sessions", 0) > 0:
            avg_mins = int(baseline.get("avg_session_duration_seconds", 0) // 60)
            explanations.append(
                {
                    "category": "Habitual Logoff Baseline",
                    "badge": "Historical Baseline",
                    "badge_class": "bg-secondary",
                    "text": f"Member historical baseline shows an average comms duration of {avg_mins} minutes across {baseline['total_sessions']} sessions.",
                }
            )
        else:
            explanations.append(
                {
                    "category": "Data Scarcity",
                    "badge": "Sparse Baseline",
                    "badge_class": "bg-secondary",
                    "text": "Limited historical session baseline available for this pilot; anomaly ratings should be interpreted conservatively.",
                }
            )

        # 4. Roster Administrative Omission
        if operation and not operation.participants.filter(user=user).exists():
            explanations.append(
                {
                    "category": "Roster Incompletion",
                    "badge": "FC Roster Omission",
                    "badge_class": "bg-light text-dark",
                    "text": "Absence from FC roster may be an administrative oversight or late fleet join rather than unauthorized comms presence.",
                }
            )

        # 5. Discord Chat Activity Telemetry Context
        try:
            # AA BigBrother
            from aa_bb.models import UserStatus

            last_msg_at = UserStatus.get_last_discord_message_at_for_user(user)
            if last_msg_at:
                if operation and operation.start_time:
                    op_start = operation.start_time - timedelta(hours=2)
                    op_end = (operation.end_time or operation.start_time) + timedelta(
                        hours=2
                    )
                    if op_start <= last_msg_at <= op_end:
                        explanations.append(
                            {
                                "category": "Text Communication Presence",
                                "badge": "Active Discord Chat",
                                "badge_class": "bg-success text-white",
                                "text": f"Member was active in Discord text chat near this operational window ({last_msg_at:%Y-%m-%d %H:%M} UTC), indicating continued organizational engagement.",
                            }
                        )
                else:
                    days_ago = (timezone.now() - last_msg_at).days
                    if days_ago <= 7:
                        explanations.append(
                            {
                                "category": "Text Communication Presence",
                                "badge": "Recent Discord Activity",
                                "badge_class": "bg-info text-dark",
                                "text": f"Member has recent Discord text activity ({last_msg_at:%Y-%m-%d %H:%M} UTC), confirming active text communication.",
                            }
                        )
            else:
                explanations.append(
                    {
                        "category": "Communication Profile",
                        "badge": "No Discord Chat",
                        "badge_class": "bg-warning text-dark",
                        "text": "No Discord text message activity recorded for this member (pilot communicates primarily via voice or remains passive in text channels).",
                    }
                )
        except Exception:
            pass

        return explanations

    @classmethod
    def generate_case_findings(cls, investigation: CIInvestigation) -> str:
        """
        Synthesize multi-source evidence into structured Markdown findings,
        prominently highlighting plausible legitimate explanations at the top
        to eliminate cognitive burden and bias during incident review.
        """
        user = investigation.subject_user
        operation = investigation.operation

        # Fetch correlations
        if operation:
            correlations = list(
                CICorrelation.objects.filter(operation=operation, user=user)
            )
        else:
            correlations = list(
                CICorrelation.objects.filter(user=user).order_by("-computed_at")[:5]
            )

        # Fetch personal baseline and benign explanations
        baseline = BaselineEngine.calculate_personal_baseline(user)
        benign_explanations = cls.get_benign_explanations(investigation)

        # Check existing overrides
        overrides = list(CIOverride.objects.filter(user=user))

        # Build synthesized Markdown report
        lines = []
        op_title = operation.name if operation else "General Activity Review"
        lines.append(f"### Investigation Case Summary: {user.username} @ {op_title}")
        lines.append("")
        lines.append(
            f"- **Subject**: `{user.username}` (Main: `{getattr(getattr(user, 'profile', None), 'main_character', None) or 'Unknown'}`)"
        )
        lines.append(
            f"- **Operation**: `{op_title}` (Type: `{operation.operation_type if operation else 'N/A'}`, Secrecy: `{operation.secrecy_level if operation else 'Standard'}`)"
        )
        lines.append(f"- **Case Status**: `{investigation.get_status_display()}`")
        lines.append(
            f"- **Synthesis Timestamp**: `{timezone.now():%Y-%m-%d %H:%M:%S UTC}`"
        )
        lines.append("")

        # Standardized "Plausible Benign Explanations" Box right at the top
        lines.append("#### ┌────────────────────────────────────────────────────────┐")
        lines.append(
            "#### │ Standardized Plausible Benign Explanations & Alternative Factors │"
        )
        lines.append("#### └────────────────────────────────────────────────────────┘")
        lines.append("##### Alternative Explanations & Confounding Factors")
        if benign_explanations:
            for b in benign_explanations:
                lines.append(f"- **[{b['badge']}]** ({b['category']}): {b['text']}")
        else:
            lines.append(
                "- No common benign confounding factors identified in telemetry."
            )
        lines.append("")

        # 1. Primary Observations
        lines.append("#### 1. Primary Observations & Correlated Anomalies")
        if correlations:
            for c in correlations:
                lines.append(
                    f"- **{c.anomaly_type}** (Confidence: `{c.confidence:.2f}`, Completeness: `{c.data_completeness:.2f}`):"
                )
                lines.append(f"  - {c.explanation}")
        else:
            lines.append(
                "- No high-confidence statistical anomalies flagged for this operational window."
            )
        lines.append("")

        # 2. Baseline & Historical Context
        lines.append("#### 2. Baseline & Historical Activity Context")
        lines.append(
            f"- 60-Day Observed Comms Sessions: `{baseline['total_sessions']}` (Avg Duration: `{int(baseline['avg_session_duration_seconds'] // 60)}` mins)"
        )
        lines.append(
            f"- 60-Day Confirmed Fleet Participations: `{baseline['total_fleets']}`"
        )

        try:
            # AA BigBrother
            from aa_bb.models import UserStatus

            last_msg_at = UserStatus.get_last_discord_message_at_for_user(user)
            if last_msg_at:
                lines.append(
                    f"- Discord Text Chat Activity: Last message sent `{last_msg_at:%Y-%m-%d %H:%M:%S UTC}`"
                )
            else:
                lines.append(
                    "- Discord Text Chat Activity: `None recorded (no text messages sent)`"
                )
        except Exception:
            pass

        if overrides:
            lines.append(
                f"- Documented Overrides / Exemptions: `{len(overrides)}` active policy exemption(s)"
            )
            for ovr in overrides:
                lines.append(
                    f"  - Exemption: `{', '.join(ovr.exempt_anomaly_types)}` (Reason: {ovr.reason})"
                )
        lines.append("")

        # 3. Recommended Reviewer Actions & One-Click Dispositions
        lines.append("#### 3. Recommended Investigator Next Steps & Quick Dispositions")
        lines.append(
            "1. **Clear (Verified Alt / Scout)**: If subject was performing recon, cyno, or hauling."
        )
        lines.append(
            "2. **Clear (Routine Habitual Logoff)**: If disconnect matches habitual baseline."
        )
        lines.append(
            "3. **Clear (Incomplete Roster Submission)**: If FC omitted member from roster."
        )
        lines.append(
            "4. **Escalate for Senior Review**: If unresolved operational security concerns persist."
        )

        report = "\n".join(lines)
        investigation.agent_summary = report
        investigation.save(update_fields=["agent_summary", "updated_at"])
        return report

    @classmethod
    def generate_case_dossier(cls, subject_user: User) -> dict:
        """
        Aggregates all operations, correlations, voice telemetry, alt characters,
        and investigation records for a member into a unified, cryptographically-hashed dossier.
        """
        # Standard Library
        import hashlib
        import json

        # Fetch character alts
        characters = []
        for co in subject_user.character_ownerships.select_related("character").all():
            char = co.character
            if char:
                characters.append(
                    {
                        "character_id": char.character_id,
                        "character_name": char.character_name,
                        "corporation_id": char.corporation_id,
                        "corporation_name": char.corporation_name,
                        "alliance_id": char.alliance_id,
                        "alliance_name": char.alliance_name,
                    }
                )

        # Baseline & Voice Telemetry
        baseline = BaselineEngine.calculate_personal_baseline(subject_user)
        total_sessions = CISession.objects.filter(user=subject_user).count()

        # Correlations
        correlations_qs = (
            CICorrelation.objects.filter(user=subject_user)
            .select_related("operation")
            .order_by("-computed_at")
        )
        correlations_data = []
        for c in correlations_qs[:50]:
            correlations_data.append(
                {
                    "operation_id": c.operation_id,
                    "operation_name": c.operation.name if c.operation else None,
                    "anomaly_type": c.anomaly_type,
                    "confidence": c.confidence,
                    "risk_score": c.risk_score,
                    "explanation": c.explanation,
                    "computed_at": c.computed_at.isoformat() if c.computed_at else None,
                }
            )

        # Investigations
        investigations_qs = (
            CIInvestigation.objects.filter(subject_user=subject_user)
            .select_related("operation")
            .order_by("-created_at")
        )
        investigations_data = []
        for inv in investigations_qs:
            investigations_data.append(
                {
                    "id": inv.id,
                    "operation_id": inv.operation_id,
                    "operation_name": inv.operation.name if inv.operation else None,
                    "status": inv.status,
                    "resolution_tag": inv.resolution_tag,
                    "dossier_hash": inv.dossier_hash,
                    "created_at": (
                        inv.created_at.isoformat() if inv.created_at else None
                    ),
                }
            )

        # UserStatus / Discord activity
        # AA BigBrother
        from aa_bb.models import UserStatus

        last_discord = UserStatus.get_last_discord_message_at_for_user(subject_user)

        # Overall composite risk score
        risk_score, factors = BaselineEngine.calculate_user_risk_score(
            subject_user, findings=correlations_data
        )

        if risk_score >= 0.70:
            risk_tier = "HIGH"
            risk_class = "danger"
        elif risk_score >= 0.40:
            risk_tier = "MEDIUM"
            risk_class = "warning"
        else:
            risk_tier = "LOW"
            risk_class = "success"

        main_char = getattr(
            getattr(subject_user, "profile", None), "main_character", None
        )

        dossier = {
            "subject_username": subject_user.username,
            "subject_user_id": subject_user.id,
            "main_character": str(main_char) if main_char else "None",
            "state": str(
                getattr(getattr(subject_user, "profile", None), "state", "None")
            ),
            "characters": characters,
            "character_count": len(characters),
            "total_voice_sessions": total_sessions,
            "total_fleets": baseline.get("total_fleets", 0),
            "last_discord_message_at": (
                last_discord.isoformat() if last_discord else None
            ),
            "risk_score": risk_score,
            "risk_tier": risk_tier,
            "risk_class": risk_class,
            "factors": factors,
            "total_anomalies": len(correlations_data),
            "correlations": correlations_data,
            "investigations": investigations_data,
            "generated_at": timezone.now().isoformat(),
        }

        raw_canonical = json.dumps(dossier, sort_keys=True)
        dossier["integrity_hash"] = hashlib.sha256(
            raw_canonical.encode("utf-8")
        ).hexdigest()
        return dossier

    @classmethod
    def auto_triage_operation(cls, operation: CIOperation) -> list[CIInvestigation]:
        """
        Run correlation on an operation and automatically triage cases for members
        with anomalies exceeding the triage confidence threshold (>= 0.50).
        """
        correlations = CorrelationEngine.correlate_operation(operation)
        triaged_cases = []

        flagged_users = {c.user for c in correlations if c.confidence >= 0.50}
        # Leave cases a reviewer has already resolved untouched
        resolved_user_ids = set(
            CIInvestigation.objects.filter(
                operation=operation,
                status__in=("cleared", "dismissed", "escalated"),
            ).values_list("subject_user_id", flat=True)
        )
        for u in flagged_users:
            if u.id in resolved_user_ids:
                continue
            case = cls.create_or_update_investigation(
                subject_user=u,
                operation=operation,
                auto_synthesize=True,
            )
            triaged_cases.append(case)

        return triaged_cases
