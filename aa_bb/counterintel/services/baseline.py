"""
Personal and role baseline computation engine for counterintelligence behavioral analytics.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import CIOperationParticipant, CISession


class BaselineEngine:
    """
    Computes statistical behavioral baselines for individual users and operational roles.
    """

    LOOKBACK_DAYS = 60

    @classmethod
    def calculate_personal_baseline(cls, user: User, reference_time=None) -> dict:
        """
        Calculates personal baseline metrics for the user over the lookback window.
        """
        ref_dt = reference_time or timezone.now()
        if timezone.is_naive(ref_dt):
            ref_dt = timezone.make_aware(ref_dt)
        start_cutoff = ref_dt - timedelta(days=cls.LOOKBACK_DAYS)

        sessions = CISession.objects.filter(
            user=user,
            start_time__gte=start_cutoff,
            start_time__lte=ref_dt,
        )

        session_count = sessions.count()
        total_duration = sum(s.duration_seconds for s in sessions)
        avg_session_duration = (
            (total_duration / session_count) if session_count > 0 else 0.0
        )

        # Disconnect hour distribution (0-23 UTC)
        disconnect_hours = [0] * 24
        for s in sessions:
            if s.end_time:
                disconnect_hours[s.end_time.hour] += 1

        # Participation counts
        fleet_count = CIOperationParticipant.objects.filter(
            user=user,
            recorded_at__gte=start_cutoff,
            recorded_at__lte=ref_dt,
        ).count()

        return {
            "user_id": user.id,
            "username": user.username,
            "lookback_days": cls.LOOKBACK_DAYS,
            "total_sessions": session_count,
            "avg_session_duration_seconds": avg_session_duration,
            "disconnect_hours": disconnect_hours,
            "total_fleets": fleet_count,
        }

    @classmethod
    def is_typical_disconnect_time(
        cls, user: User, timestamp, tolerance_hours: int = 1
    ) -> tuple[bool, float]:
        """
        Evaluates whether a disconnect at `timestamp` aligns with the user's habitual logoff times.
        Returns (is_typical, probability_score).
        """
        if timezone.is_naive(timestamp):
            timestamp = timezone.make_aware(timestamp)

        baseline = cls.calculate_personal_baseline(user, reference_time=timestamp)
        disconnect_hours = baseline["disconnect_hours"]
        total_disconnects = sum(disconnect_hours)

        if total_disconnects < 5:
            # Insufficient baseline data: neutrality
            return False, 0.5

        target_hour = timestamp.hour
        matching_count = 0
        for h in range(
            target_hour - tolerance_hours, target_hour + tolerance_hours + 1
        ):
            normalized_h = h % 24
            matching_count += disconnect_hours[normalized_h]

        prob = matching_count / total_disconnects
        is_typical = prob >= 0.25
        return is_typical, prob

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
