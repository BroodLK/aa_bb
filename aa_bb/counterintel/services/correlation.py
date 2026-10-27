"""
Multi-dimensional counterintelligence correlation and behavioral anomaly engine.
"""

# Standard Library
import logging
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.utils import timezone

# AA BigBrother
from aa_bb.models import BigBrotherConfig

from ..models import (
    CICorrelation,
    CIInvestigation,
    CIOperation,
    CIOperationParticipant,
    CIOverride,
)
from .baseline import BaselineEngine
from .operations import CIOperationService

logger = logging.getLogger(__name__)


class CorrelationEngine:
    """
    Evaluates multi-source activity against operational windows, personal baselines,
    alt character identities, and role contexts to generate versioned, reproducible
    correlation findings and composite risk indices.
    """

    ALGORITHM_VERSION = "1.2.0"
    ALERT_CONFIDENCE_THRESHOLD = 0.85
    ALERT_RISK_THRESHOLD = 0.80
    # Non-fleet sessions that began this long before the op are idle presence
    IDLE_PRESENCE_HOURS = 2
    # Without channel names we only know someone was on the server, not in fleet comms
    NO_CHANNEL_CONFIDENCE_CAP = 0.60

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

        # The timeline also displays comms-only users for officer review. Only
        # FC-confirmed rows count as fleet participation for correlation.
        confirmed_user_ids = {
            p["user_id"] for p in timeline["participants"] if p.get("confirmed_by_fc")
        }

        # 1. Comms presence without PAP credit
        findings.extend(
            cls._detect_comms_without_pap(
                operation, timeline, confirmed_user_ids, algorithm_version
            )
        )

        # 2. Pre-contact disconnect detection
        findings.extend(
            cls._detect_pre_contact_disconnects(
                operation, timeline, confirmed_user_ids, algorithm_version
            )
        )

        # 3. Briefing attendance without deployment
        findings.extend(
            cls._detect_briefing_anomalies(operation, timeline, algorithm_version)
        )

        # 4. Role-aware combat participation check
        findings.extend(
            cls._detect_combat_discrepancies(
                operation, timeline, confirmed_user_ids, algorithm_version
            )
        )

        # 5. Multi-Character & Alt-Identity Correlation
        findings.extend(
            cls._detect_alt_operational_proximity(
                operation, timeline, confirmed_user_ids, algorithm_version
            )
        )
        findings.extend(
            cls._detect_alt_in_hostile_entity(operation, timeline, algorithm_version)
        )
        findings.extend(
            cls._detect_alt_shared_voice_identity(
                operation, timeline, algorithm_version
            )
        )

        # 6. Co-occurrence & Fleet Leak Ring Detection
        findings.extend(
            cls._detect_co_occurring_disconnect_rings(
                operation, timeline, confirmed_user_ids, algorithm_version
            )
        )

        # Filter out findings suppressed by documented CIOverrides
        persisted_findings = []
        user_overrides_map = {}

        # Group findings by user for risk score calculation
        findings_by_user = {}
        for f in findings:
            findings_by_user.setdefault(f["user"].id, []).append(f)

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
                logger.info(
                    f"Anomaly {anomaly_type} for user {user.username} suppressed by active CIOverride."
                )
                continue

            # Calculate composite Bayesian risk score and factor breakdown
            user_findings = findings_by_user.get(user.id, [finding])
            risk_score, factors = BaselineEngine.calculate_user_risk_score(
                user, operation=operation, findings=user_findings
            )

            previous = CICorrelation.objects.filter(
                operation=operation, user=user, anomaly_type=anomaly_type
            ).first()
            was_alerting = previous is not None and cls._is_alerting(previous)

            corr, created = CICorrelation.objects.update_or_create(
                operation=operation,
                user=user,
                anomaly_type=anomaly_type,
                defaults={
                    "confidence": finding["confidence"],
                    "risk_score": risk_score,
                    "factors": factors,
                    "data_completeness": finding["data_completeness"],
                    "algorithm_version": algorithm_version,
                    "source_event_ids": finding["source_event_ids"],
                    "explanation": finding["explanation"],
                },
            )
            persisted_findings.append(corr)

            # Alert once, when a finding first crosses the high-confidence threshold
            if cls._is_alerting(corr) and not was_alerting:
                cls.notify_high_confidence_finding(corr)

        cls._prune_stale_correlations(operation, persisted_findings)
        return persisted_findings

    @classmethod
    def _is_alerting(cls, corr: CICorrelation) -> bool:
        return (
            corr.confidence >= cls.ALERT_CONFIDENCE_THRESHOLD
            or corr.risk_score >= cls.ALERT_RISK_THRESHOLD
        )

    @classmethod
    def _prune_stale_correlations(
        cls, operation: CIOperation, persisted: list[CICorrelation]
    ):
        """
        Remove findings this run no longer produces (data corrected, override added),
        keeping any that back an investigation case.
        """
        investigated_user_ids = CIInvestigation.objects.filter(
            operation=operation
        ).values_list("subject_user_id", flat=True)
        CICorrelation.objects.filter(operation=operation).exclude(
            id__in=[c.id for c in persisted]
        ).exclude(user_id__in=investigated_user_ids).delete()

    @classmethod
    def _is_operational_session(
        cls, session: dict, operation: CIOperation, confirmed_user_ids: set
    ) -> bool:
        """
        A session counts towards the op if its user was on the FC roster, or it
        began close enough to the op to be more than all-day idle presence.
        """
        if session["user_id"] in confirmed_user_ids:
            return True
        idle_cutoff = operation.start_time - timedelta(hours=cls.IDLE_PRESENCE_HOURS)
        return session["start_time"] >= idle_cutoff

    @classmethod
    def _detect_comms_without_pap(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        op_start, op_end = CIOperationService.get_operational_window(operation, 0, 0)
        now = timezone.now()
        user_sessions = {}
        for s in timeline["sessions"]:
            if not cls._is_operational_session(s, operation, confirmed_user_ids):
                continue
            uid = s["user_id"]
            user_sessions.setdefault(uid, []).append(s)

        for uid, sessions in user_sessions.items():
            if uid not in confirmed_user_ids:
                # Only time spent inside the operation window counts
                total_duration = sum(
                    max(
                        0.0,
                        (
                            min(op_end, s["end_time"] or now)
                            - max(op_start, s["start_time"])
                        ).total_seconds(),
                    )
                    for s in sessions
                )
                # Significant presence threshold: > 5 minutes
                if total_duration >= 300:
                    user = User.objects.filter(id=uid).first()
                    if not user:
                        continue
                    confidence = min(0.95, 0.4 + (total_duration / 3600.0) * 0.5)
                    if not any(s["channel_name"] for s in sessions):
                        confidence = min(confidence, cls.NO_CHANNEL_CONFIDENCE_CAP)
                    source_ids = [f"session:{s['id']}" for s in sessions]
                    channels = (
                        ", ".join(filter(None, {s["channel_name"] for s in sessions}))
                        or "General Comms"
                    )
                    explanation = (
                        f"User {user.username} observed in operational comms ({channels}) for "
                        f"{int(total_duration // 60)} minutes without confirmed FC fleet credit."
                    )
                    findings.append(
                        {
                            "user": user,
                            "anomaly_type": "comms_without_pap",
                            "confidence": round(confidence, 2),
                            "data_completeness": 0.95,
                            "source_event_ids": source_ids,
                            "explanation": explanation,
                        }
                    )

        return findings

    @classmethod
    def _detect_pre_contact_disconnects(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
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
            if not cls._is_operational_session(s, operation, confirmed_user_ids):
                continue
            end_time = s["end_time"]
            if end_time and critical_window_start <= end_time <= first_contact:
                user = User.objects.filter(id=s["user_id"]).first()
                if not user:
                    continue

                minutes_before = int((first_contact - end_time).total_seconds() // 60)
                is_typical, habit_prob = BaselineEngine.is_typical_disconnect_time(
                    user, end_time
                )

                # Calibrate confidence based on historical habit
                if is_typical:
                    confidence = 0.25  # Low confidence: habit explains logoff
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact, "
                        f"matching their habitual daily logoff schedule ({int(habit_prob * 100)}% historical frequency)."
                    )
                else:
                    confidence = 0.85  # High confidence: statistical outlier
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact. "
                        f"This logoff is an outlier relative to their historical activity baseline."
                    )

                findings.append(
                    {
                        "user": user,
                        "anomaly_type": "pre_contact_disconnect",
                        "confidence": round(confidence, 2),
                        "data_completeness": 0.90,
                        "source_event_ids": [
                            f"session:{s['id']}",
                            f"zkill:{combat_events[0]['id']}",
                        ],
                        "explanation": explanation,
                    }
                )

        return findings

    @classmethod
    def _detect_briefing_anomalies(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        findings = []
        briefing_objectives = [
            o for o in timeline["objectives"] if o["objective_type"] == "briefing"
        ]
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
                findings.append(
                    {
                        "user": user,
                        "anomaly_type": "briefing_without_deployment",
                        "confidence": 0.70,
                        "data_completeness": 0.85,
                        "source_event_ids": [f"session:{s['id']}"],
                        "explanation": (
                            f"User {user.username} attended sensitive strategic briefing ({operation.name}) "
                            f"without subsequent fleet deployment over the following 7 days."
                        ),
                    }
                )

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
            logger.info(
                f"Operation {operation.name} marked with support/exempt roles; suppressing combat absence check."
            )
            return findings

        combat_user_ids = {e["user_id"] for e in combat_events if e["user_id"]}
        missing_combat_users = confirmed_user_ids - combat_user_ids

        for uid in missing_combat_users:
            user = User.objects.filter(id=uid).first()
            if not user:
                continue

            findings.append(
                {
                    "user": user,
                    "anomaly_type": "combat_absence_unexplained",
                    "confidence": 0.45,
                    "data_completeness": 0.80,
                    "source_event_ids": [f"op:{operation.id}"],
                    "explanation": (
                        f"User {user.username} was confirmed on combat fleet roster but did not appear on "
                        f"any of the {len(combat_events)} recorded killmails for this engagement."
                    ),
                }
            )

        return findings

    @classmethod
    def _detect_alt_operational_proximity(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
        algorithm_version: str,
    ) -> list[dict]:
        """
        Flags when an uncredited alt character belonging to a fleet participant or comms attendee
        was co-located in operational solar systems or combat areas during the active fleet window.
        """
        findings = []
        combat_events = [e for e in timeline["events"] if e["source"] == "zkill"]
        op_systems = set()
        for e in combat_events:
            sys_id = e.get("payload", {}).get("solar_system_id")
            if sys_id:
                op_systems.add(sys_id)

        all_user_ids = confirmed_user_ids | {s["user_id"] for s in timeline["sessions"]}

        for uid in all_user_ids:
            user = (
                User.objects.filter(id=uid)
                .prefetch_related("character_ownerships__character")
                .first()
            )
            if not user:
                continue

            user_char_ids = {
                co.character.character_id
                for co in user.character_ownerships.all()
                if co.character
            }
            credited_char_ids = {
                p["character_id"]
                for p in timeline["participants"]
                if p["user_id"] == uid
            }
            uncredited_alts = user_char_ids - credited_char_ids

            if not uncredited_alts:
                continue

            # Check if uncredited alt appears in combat events in operational systems
            for ev in combat_events:
                p_load = ev.get("payload", {})
                sys_id = p_load.get("solar_system_id")
                vic_id = p_load.get("victim_character_id")
                att_ids = set(p_load.get("attacker_character_ids", []))

                alt_matched = uncredited_alts.intersection({vic_id} | att_ids)
                if alt_matched:
                    matched_id = list(alt_matched)[0]
                    findings.append(
                        {
                            "user": user,
                            "anomaly_type": "alt_operational_proximity",
                            "confidence": 0.85,
                            "data_completeness": 0.90,
                            "source_event_ids": [f"zkill:{ev.get('id', '')}"],
                            "explanation": (
                                f"User {user.username}'s uncredited alt character ({matched_id}) was active "
                                f"in operational solar system {sys_id} during {operation.name}."
                            ),
                        }
                    )
                    break

        return findings

    @classmethod
    def _detect_alt_in_hostile_entity(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        """
        Flags when a member present in strategic voice comms or fleet rosters maintains
        an alt character in a designated hostile corporation or alliance.
        """
        findings = []
        try:
            cfg = BigBrotherConfig.get_solo()
            hostile_corps = {
                int(x.strip())
                for x in (cfg.hostile_corporations or "").split(",")
                if x.strip().isdigit()
            }
            hostile_alliances = {
                int(x.strip())
                for x in (cfg.hostile_alliances or "").split(",")
                if x.strip().isdigit()
            }
        except Exception:
            return findings

        if not hostile_corps and not hostile_alliances:
            return findings

        active_user_ids = {p["user_id"] for p in timeline["participants"]} | {
            s["user_id"] for s in timeline["sessions"]
        }

        for uid in active_user_ids:
            user = (
                User.objects.filter(id=uid)
                .prefetch_related("character_ownerships__character")
                .first()
            )
            if not user:
                continue

            for co in user.character_ownerships.all():
                char = co.character
                if not char:
                    continue

                is_hostile_corp = char.corporation_id in hostile_corps
                is_hostile_ally = char.alliance_id in hostile_alliances

                if is_hostile_corp or is_hostile_ally:
                    entity_str = (
                        f"hostile alliance ({char.alliance_name or char.alliance_id})"
                        if is_hostile_ally
                        else f"hostile corporation ({char.corporation_name or char.corporation_id})"
                    )
                    findings.append(
                        {
                            "user": user,
                            "anomaly_type": "alt_in_hostile_entity",
                            "confidence": 0.90,
                            "data_completeness": 0.95,
                            "source_event_ids": [f"char:{char.character_id}"],
                            "explanation": (
                                f"User {user.username} has alt character '{char.character_name}' "
                                f"affiliated with {entity_str} while attending operational activity."
                            ),
                        }
                    )
                    break

        return findings

    @classmethod
    def _detect_alt_shared_voice_identity(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        """
        Flags when multiple distinct Alliance Auth users share identical Mumble usernames,
        indicating shared credentials or unvetted multi-boxing across voice endpoints.
        """
        findings = []
        mumble_events = [
            e
            for e in timeline["events"]
            if e["source"] == "mumble" and e.get("payload", {}).get("username")
        ]

        username_to_users = {}
        for ev in mumble_events:
            uname = ev["payload"]["username"].strip().lower()
            uid = ev.get("user_id")
            if uname and uid:
                username_to_users.setdefault(uname, set()).add(uid)

        for uname, uids in username_to_users.items():
            if len(uids) > 1:
                users = list(User.objects.filter(id__in=uids))
                user_names = ", ".join(u.username for u in users)
                for u in users:
                    findings.append(
                        {
                            "user": u,
                            "anomaly_type": "alt_shared_mumble_identity",
                            "confidence": 0.85,
                            "data_completeness": 0.90,
                            "source_event_ids": [f"mumble:{uname}"],
                            "explanation": (
                                f"Multiple Alliance Auth accounts ({user_names}) authenticated "
                                f"using identical Mumble handle '{uname}' during {operation.name}."
                            ),
                        }
                    )

        return findings

    @classmethod
    def _detect_co_occurring_disconnect_rings(
        cls,
        operation: CIOperation,
        timeline: dict,
        confirmed_user_ids: set,
        algorithm_version: str,
    ) -> list[dict]:
        """
        Detects leak rings: groups of 2 or more users who systematically drop voice comms
        simultaneously (within 5 minutes) prior to hostile combat contact across multiple operations.
        """
        findings = []
        combat_events = [e for e in timeline["events"] if e["source"] == "zkill"]
        if not combat_events:
            return findings

        first_contact = min(e["timestamp"] for e in combat_events)
        critical_window_start = first_contact - timedelta(minutes=15)

        # Users who disconnected in the critical window for this op
        curr_op_disconnect_uids = set()
        for s in timeline["sessions"]:
            if not cls._is_operational_session(s, operation, confirmed_user_ids):
                continue
            end_time = s["end_time"]
            if end_time and critical_window_start <= end_time <= first_contact:
                curr_op_disconnect_uids.add(s["user_id"])

        if len(curr_op_disconnect_uids) < 2:
            return findings

        # Look back at historical pre-contact disconnect correlations over the past 30 days
        lookback_cutoff = operation.start_time - timedelta(days=30)
        past_disconnect_correlations = (
            CICorrelation.objects.filter(
                anomaly_type="pre_contact_disconnect",
                operation__start_time__gte=lookback_cutoff,
                operation__start_time__lt=operation.start_time,
            )
            .exclude(operation=operation)
            .values("operation_id", "user_id")
        )

        op_to_users = {}
        for row in past_disconnect_correlations:
            op_to_users.setdefault(row["operation_id"], set()).add(row["user_id"])

        # Check for co-occurrence pair in at least 1 prior operation
        co_occurring_pairs = set()
        disconnect_uids_list = list(curr_op_disconnect_uids)
        for i in range(len(disconnect_uids_list)):
            for j in range(i + 1, len(disconnect_uids_list)):
                u1 = disconnect_uids_list[i]
                u2 = disconnect_uids_list[j]
                # Check prior operations where both u1 and u2 dropped comms
                shared_prior_ops = [
                    op_id
                    for op_id, u_set in op_to_users.items()
                    if u1 in u_set and u2 in u_set
                ]
                if shared_prior_ops:
                    co_occurring_pairs.add(u1)
                    co_occurring_pairs.add(u2)

        if co_occurring_pairs:
            ring_users = list(User.objects.filter(id__in=co_occurring_pairs))
            ring_names = ", ".join(u.username for u in ring_users)
            for u in ring_users:
                findings.append(
                    {
                        "user": u,
                        "anomaly_type": "co_occurring_disconnect_ring",
                        "confidence": 0.88,
                        "data_completeness": 0.90,
                        "source_event_ids": [f"op:{operation.id}"],
                        "explanation": (
                            f"User {u.username} demonstrated synchronized pre-contact voice comms disconnects "
                            f"with member(s) ({ring_names}) across multiple operational windows."
                        ),
                    }
                )

        return findings

    @classmethod
    def notify_high_confidence_finding(cls, correlation: CICorrelation):
        """
        Sends an automated Discord webhook alert for high-confidence anomalies.
        """
        try:
            # AA BigBrother
            from aa_bb.app_settings import send_status_embed

            cfg = BigBrotherConfig.get_solo()
            webhook = getattr(cfg, "ci_webhook", None) or cfg.webhook
            if not webhook:
                return

            user = correlation.user
            op_name = (
                correlation.operation.name
                if correlation.operation
                else "General Operation"
            )

            fields = [
                {
                    "name": "Subject Member",
                    "value": f"**{user.username}**",
                    "inline": True,
                },
                {"name": "Operation", "value": op_name, "inline": True},
                {
                    "name": "Anomaly Type",
                    "value": f"`{correlation.anomaly_type}`",
                    "inline": True,
                },
                {
                    "name": "Confidence Score",
                    "value": f"**{int(correlation.confidence * 100)}%**",
                    "inline": True,
                },
                {
                    "name": "Risk Index",
                    "value": f"**{int(correlation.risk_score * 100)}%**",
                    "inline": True,
                },
                {
                    "name": "Algorithm Version",
                    "value": correlation.algorithm_version,
                    "inline": True,
                },
                {
                    "name": "Evidence Summary",
                    "value": correlation.explanation[:1000],
                    "inline": False,
                },
            ]

            send_status_embed(
                title="🚨 Counterintelligence High-Confidence Finding",
                description=f"Automated correlation flagged anomalous behavior for **{user.username}** during **{op_name}**.",
                color=15158332,  # Crimson Red
                fields=fields,
                hook=webhook,
                footer_text="Alliance Auth BigBrother · Counterintelligence Engine",
            )
        except Exception as e:
            logger.warning(f"Could not send CI high-confidence finding webhook: {e}")
