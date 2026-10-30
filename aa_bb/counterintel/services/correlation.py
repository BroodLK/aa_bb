"""
Multi-dimensional counterintelligence correlation and behavioral anomaly engine.
"""

# Standard Library
import logging
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.core.cache import cache
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
from ..scoring import scoring
from .baseline import BaselineEngine
from .bb_signals import BBSignals
from .operations import CIOperationService

logger = logging.getLogger(__name__)


class CorrelationEngine:
    """
    Evaluates multi-source activity against operational windows, personal baselines,
    alt character identities, and role contexts to generate versioned, reproducible
    correlation findings and composite risk indices.
    """

    # Confidences, thresholds and weights are admin-editable scoring settings
    # (aa_bb.counterintel.scoring); the constants here are structural.
    ALGORITHM_VERSION = "1.2.0"
    # Non-fleet sessions that began this long before the op are idle presence
    IDLE_PRESENCE_HOURS = 2
    # Briefing attendees are expected to deploy within this many days
    BRIEFING_DEPLOYMENT_DAYS = 7

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

        # 7. Fleet characters hidden from corptools auditing
        findings.extend(
            cls._detect_unaudited_fleet_characters(
                operation, timeline, algorithm_version
            )
        )

        # 8. In-game login/logoff activity around the operation
        findings.extend(
            cls._detect_uncredited_alt_online(operation, timeline, confirmed_user_ids)
        )
        findings.extend(cls._detect_eve_logoff_before_contact(operation, timeline))

        # 9. Big Brother flags on members with access to sensitive operations
        findings.extend(
            cls._detect_bb_flagged_in_sensitive_op(
                operation, timeline, confirmed_user_ids
            )
        )

        # 10. Members disproportionately exposed to operations marked as leaked
        findings.extend(cls._detect_leak_exposure(operation))

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
                user_overrides_map[user.id] = list(
                    CIOverride.objects.active().filter(user=user)
                )

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
        cfg = scoring()
        return (
            corr.confidence >= cfg.alert_confidence or corr.risk_score >= cfg.alert_risk
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
        cfg = scoring()
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
                    # Rises with time spent: +0.5 per hour above the base
                    confidence = min(
                        cfg.comms_max,
                        cfg.comms_base + (total_duration / 3600.0) * 0.5,
                    )
                    if not any(s["channel_name"] for s in sessions):
                        confidence = min(confidence, cfg.comms_no_channel_cap)
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
        cfg = scoring()
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
                if not BaselineEngine.has_disconnect_baseline(user, end_time):
                    confidence = cfg.conf_pre_contact_no_history
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact. "
                        f"There is not enough comms history to tell whether this is habitual."
                    )
                elif is_typical:
                    # Low confidence: habit explains logoff
                    confidence = cfg.conf_pre_contact_habitual
                    explanation = (
                        f"User {user.username} disconnected {minutes_before}m prior to hostile contact, "
                        f"matching their habitual daily logoff schedule ({int(habit_prob * 100)}% historical frequency)."
                    )
                else:
                    # High confidence: statistical outlier
                    confidence = cfg.conf_pre_contact_outlier
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
        if not briefing_objectives and not operation.is_sensitive:
            return findings

        # "No deployment over the following week" can only be judged once that
        # week has passed
        deployment_window_end = operation.effective_end() + timedelta(
            days=cls.BRIEFING_DEPLOYMENT_DAYS
        )
        if deployment_window_end > timezone.now():
            return findings

        confirmed_user_ids = {
            p["user_id"] for p in timeline["participants"] if p.get("confirmed_by_fc")
        }
        briefing_sessions = {}
        for s in timeline["sessions"]:
            if (s["duration_seconds"] or 0) >= 600 and cls._is_operational_session(
                s, operation, confirmed_user_ids
            ):
                briefing_sessions.setdefault(s["user_id"], s)
        if not briefing_sessions:
            return findings

        # Deployments to other operations in the following week, in one query
        deployed_user_ids = set(
            CIOperationParticipant.objects.filter(
                user_id__in=briefing_sessions,
                recorded_at__gte=operation.start_time,
                recorded_at__lte=deployment_window_end,
            )
            .exclude(operation=operation)
            .values_list("user_id", flat=True)
        )
        users = User.objects.in_bulk(
            [uid for uid in briefing_sessions if uid not in deployed_user_ids]
        )

        # Users present during briefing who never deployed to subsequent operations
        for uid, user in users.items():
            s = briefing_sessions[uid]
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "briefing_without_deployment",
                    "confidence": scoring().confidence("briefing_without_deployment"),
                    "data_completeness": 0.85,
                    "source_event_ids": [f"session:{s['id']}"],
                    "explanation": (
                        f"User {user.username} attended sensitive strategic briefing ({operation.name}) "
                        f"without subsequent fleet deployment over the following "
                        f"{cls.BRIEFING_DEPLOYMENT_DAYS} days."
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

        # A killmail event stores a single user, so presence is taken from each
        # participant's character appearing on any victim/attacker list
        on_killmail_user_ids = {
            p["user_id"] for p in timeline["participants"] if p.get("on_killmail")
        }
        missing_combat_users = confirmed_user_ids - on_killmail_user_ids

        for uid in missing_combat_users:
            user = User.objects.filter(id=uid).first()
            if not user:
                continue

            findings.append(
                {
                    "user": user,
                    "anomaly_type": "combat_absence_unexplained",
                    "confidence": scoring().confidence("combat_absence_unexplained"),
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
                            "confidence": scoring().confidence(
                                "alt_operational_proximity"
                            ),
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
                            "confidence": scoring().confidence("alt_in_hostile_entity"),
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
                            "confidence": scoring().confidence(
                                "alt_shared_mumble_identity"
                            ),
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
    def _operational_user_ids(
        cls, operation: CIOperation, timeline: dict, confirmed_user_ids: set
    ) -> set:
        """Roster members plus users whose comms sessions count towards the op."""
        return confirmed_user_ids | {
            s["user_id"]
            for s in timeline["sessions"]
            if cls._is_operational_session(s, operation, confirmed_user_ids)
        }

    @classmethod
    def _detect_uncredited_alt_online(
        cls, operation: CIOperation, timeline: dict, confirmed_user_ids: set
    ) -> list[dict]:
        """
        Flags fleet or comms members who logged in a character that was not on
        the FC roster during the operation. Uncredited scouts and cyno alts are
        common, so this is a mark rather than a case on its own.
        """
        op_user_ids = cls._operational_user_ids(operation, timeline, confirmed_user_ids)
        roster_char_ids = {
            p["character_id"] for p in timeline["participants"] if p["character_id"]
        }
        alts_by_user = {}
        for ev in timeline["events"]:
            payload = ev["payload"] or {}
            if (
                ev["source"] == "eve_activity"
                and payload.get("activity_type") == "login"
                and ev["user_id"] in op_user_ids
                and payload.get("character_id") not in roster_char_ids
            ):
                alts_by_user.setdefault(ev["user_id"], {})[
                    payload.get("character_id")
                ] = (
                    payload.get("character_name") or str(payload.get("character_id")),
                    ev,
                )

        users = User.objects.in_bulk(list(alts_by_user))
        findings = []
        for uid, alts in alts_by_user.items():
            user = users.get(uid)
            if not user:
                continue
            names = ", ".join(sorted(name for name, _ev in alts.values()))
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "uncredited_alt_online",
                    "confidence": scoring().confidence("uncredited_alt_online"),
                    "data_completeness": 0.80,
                    "source_event_ids": [
                        f"event:{ev['id']}" for _name, ev in alts.values()
                    ],
                    "explanation": (
                        f"User {user.username} logged in character(s) {names} during "
                        f"{operation.name} without them being on the fleet roster."
                    ),
                }
            )
        return findings

    @classmethod
    def _detect_eve_logoff_before_contact(
        cls, operation: CIOperation, timeline: dict
    ) -> list[dict]:
        """
        Flags roster characters that logged off in-game in the 15 minutes before
        the first killmail. No in-game logoff baseline exists yet, so this stays
        below auto-triage.
        """
        combat_events = [e for e in timeline["events"] if e["source"] == "zkill"]
        if not combat_events:
            return []
        first_contact = min(e["timestamp"] for e in combat_events)
        critical_window_start = first_contact - timedelta(minutes=15)

        roster_char_ids = {
            p["character_id"] for p in timeline["participants"] if p["character_id"]
        }
        logoffs_by_user = {}
        for ev in timeline["events"]:
            payload = ev["payload"] or {}
            if (
                ev["source"] == "eve_activity"
                and payload.get("activity_type") == "logoff"
                and ev["user_id"]
                and payload.get("character_id") in roster_char_ids
                and critical_window_start <= ev["timestamp"] <= first_contact
            ):
                logoffs_by_user.setdefault(ev["user_id"], []).append(ev)

        users = User.objects.in_bulk(list(logoffs_by_user))
        findings = []
        for uid, logoffs in logoffs_by_user.items():
            user = users.get(uid)
            if not user:
                continue
            details = ", ".join(
                f"{ev['payload'].get('character_name') or ev['payload'].get('character_id')} "
                f"{int((first_contact - ev['timestamp']).total_seconds() // 60)}m before"
                for ev in logoffs
            )
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "eve_logoff_before_contact",
                    "confidence": scoring().confidence("eve_logoff_before_contact"),
                    "data_completeness": 0.80,
                    "source_event_ids": [f"event:{ev['id']}" for ev in logoffs],
                    "explanation": (
                        f"User {user.username}'s fleet character(s) logged off in-game "
                        f"shortly before hostile contact in {operation.name} ({details})."
                    ),
                }
            )
        return findings

    BB_SIGNAL_MIN_SEVERITY = 0.5

    @classmethod
    def _is_sensitive_operation(cls, operation: CIOperation, timeline: dict) -> bool:
        return operation.is_sensitive or any(
            o["objective_type"] == "briefing" for o in timeline["objectives"]
        )

    @classmethod
    def _detect_bb_flagged_in_sensitive_op(
        cls, operation: CIOperation, timeline: dict, confirmed_user_ids: set
    ) -> list[dict]:
        """
        Flags members carrying Big Brother hostile-tie flags (blacklists, awox,
        hostile clones/assets, suspicious contacts/contracts/mails) who had access
        to a sensitive operation.
        """
        if not cls._is_sensitive_operation(operation, timeline):
            return []
        signals = BBSignals.for_users(
            cls._operational_user_ids(operation, timeline, confirmed_user_ids)
        )
        signals = {
            uid: flags
            for uid, flags in signals.items()
            if flags[0]["severity"] >= cls.BB_SIGNAL_MIN_SEVERITY
        }
        users = User.objects.in_bulk(list(signals))
        cfg = scoring()
        findings = []
        for uid, flags in signals.items():
            user = users.get(uid)
            if not user:
                continue
            labels = ", ".join(f["label"] for f in flags)
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "bb_flagged_in_sensitive_op",
                    "confidence": round(
                        min(
                            1.0,
                            cfg.bb_sensitive_base
                            + cfg.bb_sensitive_scale * flags[0]["severity"],
                        ),
                        2,
                    ),
                    "data_completeness": 0.90,
                    "source_event_ids": [f"bb:{f['flag']}" for f in flags],
                    "explanation": (
                        f"User {user.username} had access to sensitive operation "
                        f"{operation.name} while flagged by Big Brother for: {labels}."
                    ),
                }
            )
        return findings

    LEAK_LOOKBACK_DAYS = 90
    LEAK_EXPOSURE_CACHE_SECONDS = 6 * 3600
    EXPOSURE_CACHE_VERSION_KEY = "ci:exposure_version"

    @classmethod
    def invalidate_exposure_cache(cls):
        """Drop every cached exposure set, e.g. after comms data is rebuilt."""
        try:
            cache.incr(cls.EXPOSURE_CACHE_VERSION_KEY)
        except ValueError:
            cache.set(cls.EXPOSURE_CACHE_VERSION_KEY, 2, timeout=None)

    @classmethod
    def _cached_exposure(cls, operation: CIOperation) -> set:
        """Exposure set of a concluded op, cached while its window is unchanged."""
        version = cache.get(cls.EXPOSURE_CACHE_VERSION_KEY, 1)
        end = operation.end_time.isoformat() if operation.end_time else "open"
        key = (
            f"ci:exposure:v{version}:{operation.id}:"
            f"{operation.start_time.isoformat()}:{end}"
        )
        exposed = cache.get(key)
        if exposed is None:
            exposed = CIOperationService.exposed_user_ids(operation)
            cache.set(key, exposed, timeout=cls.LEAK_EXPOSURE_CACHE_SECONDS)
        return exposed

    @classmethod
    def _detect_leak_exposure(cls, operation: CIOperation) -> list[dict]:
        """
        For an operation marked as leaked, flags members who knew about leaked
        operations far more often than about clean ones. A single leak cannot
        point at anyone; the signal comes from repeated overlap across leaks.
        """
        if not operation.leaked:
            return []

        cfg = scoring()
        since = timezone.now() - timedelta(days=cls.LEAK_LOOKBACK_DAYS)
        ops = [
            op
            for op in CIOperation.objects.filter(start_time__gte=since)
            if op.is_concluded or op.id == operation.id
        ]
        leaked_ops = [op for op in ops if op.leaked]
        clean_ops = [op for op in ops if not op.leaked]
        if len(leaked_ops) < cfg.leak_min_ops:
            return []

        exposure = {op.id: cls._cached_exposure(op) for op in ops}
        candidates = exposure.get(operation.id, set())
        users = User.objects.in_bulk(list(candidates))

        findings = []
        for uid in candidates:
            user = users.get(uid)
            if not user:
                continue
            leaked_seen = [op for op in leaked_ops if uid in exposure[op.id]]
            clean_seen = sum(1 for op in clean_ops if uid in exposure[op.id])
            a = len(leaked_seen)
            leak_rate = a / len(leaked_ops)
            # Laplace-smoothed so members with no clean attendance don't divide by 0
            clean_rate = (clean_seen + 1) / (len(clean_ops) + 1)
            lift = leak_rate / clean_rate
            if (
                a < cfg.leak_min_ops
                or leak_rate < cfg.leak_min_rate
                or lift < cfg.leak_min_lift
            ):
                continue

            # +0.10 per extra leak attended, +0.08 per unit of lift (capped at 5)
            confidence = min(
                cfg.leak_max,
                cfg.leak_base
                + 0.10 * (a - cfg.leak_min_ops)
                + 0.08 * (min(lift, 5.0) - cfg.leak_min_lift),
            )
            confidence = max(cfg.leak_base, confidence)
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "leak_exposure",
                    "confidence": round(confidence, 2),
                    "data_completeness": 0.85,
                    "source_event_ids": [f"op:{op.id}" for op in leaked_seen],
                    "explanation": (
                        f"User {user.username} knew of {a} of {len(leaked_ops)} "
                        f"operations marked as leaked in the last "
                        f"{cls.LEAK_LOOKBACK_DAYS} days, versus {clean_seen} of "
                        f"{len(clean_ops)} clean operations (lift x{lift:.1f})."
                    ),
                }
            )
        return findings

    @classmethod
    def _detect_unaudited_fleet_characters(
        cls,
        operation: CIOperation,
        timeline: dict,
        algorithm_version: str,
    ) -> list[dict]:
        """
        Flags members who flew FC-confirmed characters that have no corptools audit,
        leaving that character's assets, contacts and history unverifiable.
        """
        unaudited_by_user = {}
        for p in timeline["participants"]:
            if p.get("confirmed_by_fc") and p.get("has_audit") is False:
                unaudited_by_user.setdefault(p["user_id"], []).append(p)

        findings = []
        for uid, participants in unaudited_by_user.items():
            user = User.objects.filter(id=uid).first()
            if not user:
                continue
            names = ", ".join(p["character_name"] for p in participants)
            findings.append(
                {
                    "user": user,
                    "anomaly_type": "unaudited_fleet_character",
                    "confidence": scoring().confidence("unaudited_fleet_character"),
                    "data_completeness": 1.0,
                    "source_event_ids": [
                        f"participant:{p['id']}" for p in participants if p["id"]
                    ],
                    "explanation": (
                        f"User {user.username} flew {names} in this operation "
                        f"without a corptools audit for that character."
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
                        "confidence": scoring().confidence(
                            "co_occurring_disconnect_ring"
                        ),
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
