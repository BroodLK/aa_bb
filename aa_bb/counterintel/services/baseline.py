"""
Personal and role baseline computation engine for counterintelligence behavioral analytics.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from ..models import CIOperationParticipant, CISession
from ..scoring import scoring


class BaselineEngine:
    """
    Computes statistical behavioral baselines and multi-factor risk scores
    for individual users and operational roles.
    """

    LOOKBACK_DAYS = 60
    CACHE_TTL_SECONDS = 21600  # 6 hours
    # Fewer recorded disconnects than this cannot establish a logoff habit
    MIN_BASELINE_DISCONNECTS = 5

    @classmethod
    def has_disconnect_baseline(cls, user: User, reference_time=None) -> bool:
        """True if the user has enough disconnect history to judge habit."""
        baseline = cls.calculate_personal_baseline(user, reference_time=reference_time)
        return sum(baseline["disconnect_hours"]) >= cls.MIN_BASELINE_DISCONNECTS

    @classmethod
    def calculate_personal_baseline(cls, user: User, reference_time=None) -> dict:
        """
        Calculates personal baseline metrics for the user over the lookback window,
        caching the result to prevent redundant SQL computation and memory churn.
        """
        ref_dt = reference_time or timezone.now()
        if timezone.is_naive(ref_dt):
            ref_dt = timezone.make_aware(ref_dt)

        cache_key = f"ci_baseline_{user.id}_{ref_dt.strftime('%Y%m%d%H')}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        start_cutoff = ref_dt - timedelta(days=cls.LOOKBACK_DAYS)

        sessions_qs = CISession.objects.filter(
            user=user,
            start_time__gte=start_cutoff,
            start_time__lte=ref_dt,
        )

        session_count = 0
        total_duration = 0.0
        disconnect_hours = [0] * 24
        # Circadian grid: 7 days of week (0=Mon, 6=Sun) x 24 hours
        circadian_grid = [[0] * 24 for _ in range(7)]

        for s in sessions_qs.iterator(chunk_size=500):
            session_count += 1
            total_duration += s.duration_seconds
            if s.end_time:
                disconnect_hours[s.end_time.hour] += 1
                circadian_grid[s.end_time.weekday()][s.end_time.hour] += 1

        avg_session_duration = (
            (total_duration / session_count) if session_count > 0 else 0.0
        )

        # Participation counts
        fleet_count = CIOperationParticipant.objects.filter(
            user=user,
            recorded_at__gte=start_cutoff,
            recorded_at__lte=ref_dt,
        ).count()

        result = {
            "user_id": user.id,
            "username": user.username,
            "lookback_days": cls.LOOKBACK_DAYS,
            "total_sessions": session_count,
            "avg_session_duration_seconds": avg_session_duration,
            "disconnect_hours": disconnect_hours,
            "circadian_grid": circadian_grid,
            "total_fleets": fleet_count,
        }

        cache.set(cache_key, result, timeout=cls.CACHE_TTL_SECONDS)
        return result

    @classmethod
    def is_typical_disconnect_time(
        cls, user: User, timestamp, tolerance_hours: int = 1
    ) -> tuple[bool, float]:
        """
        Evaluates whether a disconnect at `timestamp` aligns with the user's habitual logoff times,
        incorporating weekday vs weekend circadian patterns.
        Returns (is_typical, probability_score).
        """
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)

        baseline = cls.calculate_personal_baseline(user, reference_time=timestamp)
        disconnect_hours = baseline["disconnect_hours"]
        circadian_grid = baseline.get("circadian_grid") or [[0] * 24 for _ in range(7)]
        total_disconnects = sum(disconnect_hours)

        if total_disconnects < cls.MIN_BASELINE_DISCONNECTS:
            # Insufficient baseline data: neutrality
            return False, 0.5

        target_hour = timestamp.hour
        target_day = timestamp.weekday()

        matching_count = 0
        day_matching_count = 0
        day_total = sum(circadian_grid[target_day])

        for h in range(
            target_hour - tolerance_hours, target_hour + tolerance_hours + 1
        ):
            normalized_h = h % 24
            matching_count += disconnect_hours[normalized_h]
            day_matching_count += circadian_grid[target_day][normalized_h]

        prob_overall = matching_count / total_disconnects
        prob_day = (day_matching_count / day_total) if day_total > 0 else prob_overall

        combined_prob = 0.6 * prob_day + 0.4 * prob_overall
        is_typical = combined_prob >= 0.25
        return is_typical, round(combined_prob, 3)

    @classmethod
    def calculate_user_risk_score(
        cls,
        user: User,
        operation=None,
        findings: list[dict] = None,
    ) -> tuple[float, dict]:
        """
        Calculates a Bayesian/composite risk score (0.0 to 1.0) and factor breakdown
        for a user, incorporating comms and in-game disconnect timing, Discord chat
        inactivity, alt proximity / hostile affiliation, historical deviation,
        Big Brother hostile-tie flags, leak exposure and audit accountability.
        """
        # AA BigBrother
        from aa_bb.counterintel.services.bb_signals import BBSignals

        findings = findings or []

        def max_confidence(anomaly_types) -> float:
            return max(
                [
                    f.get("confidence", 0.0)
                    for f in findings
                    if f.get("anomaly_type") in anomaly_types
                ],
                default=0.0,
            )

        # 1. Comms / in-game Disconnect & Presence factor
        comms_factor = max_confidence(
            ("pre_contact_disconnect", "comms_without_pap", "eve_logoff_before_contact")
        )

        # 2. Discord Chat Inactivity factor
        # AA BigBrother
        from aa_bb.models import UserStatus

        last_discord = UserStatus.get_last_discord_message_at_for_user(user)
        discord_factor = 0.0
        if last_discord:
            days_since_msg = (timezone.now() - last_discord).total_seconds() / 86400.0
            if days_since_msg >= 30:
                discord_factor = 0.8
            elif days_since_msg >= 14:
                discord_factor = 0.5
            elif days_since_msg >= 7:
                discord_factor = 0.25
        else:
            # Never sent a message
            discord_factor = 0.6

        # 3. Alt Spatial Proximity / Hostile Affiliation factor
        alt_factor = max_confidence(
            (
                "alt_operational_proximity",
                "alt_in_hostile_entity",
                "alt_shared_mumble_identity",
                "uncredited_alt_online",
            )
        )

        # 4. Historical / Circadian Deviation factor
        deviation_factor = max_confidence(("pre_contact_disconnect",))

        # 5. Big Brother hostile-tie flags, independent of this operation
        bb_factor = BBSignals.combined(BBSignals.for_user(user.id))

        # 6. Repeated exposure to leaked operations
        leak_factor = max_confidence(("leak_exposure",))

        # 7. Flying characters hidden from corptools auditing
        accountability_factor = max_confidence(("unaudited_fleet_character",))

        weights = scoring().risk_weights()
        raw_score = sum(
            weight * factor
            for weight, factor in (
                (weights["comms"], comms_factor),
                (weights["discord"], discord_factor),
                (weights["alt"], alt_factor),
                (weights["deviation"], deviation_factor),
                (weights["bb_signals"], bb_factor),
                (weights["leak"], leak_factor),
                (weights["accountability"], accountability_factor),
            )
        )

        # Secrecy level scaling multiplier
        secrecy_level = (
            getattr(operation, "secrecy_level", "standard") if operation else "standard"
        )
        multiplier = operation.secrecy_multiplier if operation else 1.0

        final_risk = min(1.0, raw_score * multiplier)

        factors = {
            "comms_disconnect_timing": round(comms_factor, 2),
            "discord_inactivity": round(discord_factor, 2),
            "alt_proximity_and_hostility": round(alt_factor, 2),
            "historical_deviation": round(deviation_factor, 2),
            "bb_signals": round(bb_factor, 2),
            "leak_exposure": round(leak_factor, 2),
            "audit_accountability": round(accountability_factor, 2),
            "secrecy_multiplier": multiplier,
            "secrecy_level": secrecy_level,
        }

        return round(final_risk, 2), factors

    @classmethod
    def is_role_exempt_from_combat(cls, expected_roles: list) -> bool:
        """
        Determines if an assigned or expected role is non-combat support (Logi, Recon, Cyno, Briefing).
        """
        support_roles = {
            "logi",
            "logistics",
            "recon",
            "scout",
            "cyno",
            "briefing",
            "hauler",
            "leadership",
        }
        roles_set = {str(r).lower() for r in (expected_roles or [])}
        return bool(roles_set.intersection(support_roles))
