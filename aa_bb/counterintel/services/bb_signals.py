"""
Bridge from Big Brother's per-member checks (UserStatus) into counterintel scoring.
"""

# AA BigBrother
from aa_bb.models import UserStatus


class BBSignals:
    """
    Reads the cached Big Brother flags for members and weights the ones that
    indicate hostile ties. Readiness checks (cyno, skills) are not spy indicators.
    """

    # UserStatus flag -> (label, severity 0..1)
    FLAG_SEVERITIES = {
        "has_alliance_blacklist": ("Alliance blacklist", 1.0),
        "has_coalition_blacklist": ("Coalition blacklist", 1.0),
        "has_awox_kills": ("Awox kills", 0.9),
        "has_hostile_clones": ("Hostile clones", 0.7),
        "has_sus_contacts": ("Suspicious contacts", 0.6),
        "has_sus_contracts": ("Suspicious contracts", 0.6),
        "has_sus_mails": ("Suspicious mails", 0.6),
        "has_hostile_assets": ("Hostile assets", 0.5),
        "has_sus_trans": ("Suspicious transactions", 0.5),
    }

    @classmethod
    def for_users(cls, user_ids) -> dict:
        """{user_id: [{"flag", "label", "severity"}, ...]} for flagged users."""
        user_ids = [uid for uid in user_ids if uid]
        if not user_ids:
            return {}
        rows = UserStatus.objects.filter(user_id__in=user_ids).values(
            "user_id", *cls.FLAG_SEVERITIES
        )
        signals = {}
        for row in rows:
            flags = [
                {"flag": flag, "label": label, "severity": severity}
                for flag, (label, severity) in cls.FLAG_SEVERITIES.items()
                if row.get(flag)
            ]
            if flags:
                flags.sort(key=lambda f: -f["severity"])
                signals[row["user_id"]] = flags
        return signals

    @classmethod
    def for_user(cls, user_id) -> list:
        return cls.for_users([user_id]).get(user_id, [])

    @staticmethod
    def combined(signals: list) -> float:
        """Noisy-OR of flag severities: several moderate flags add up."""
        remaining = 1.0
        for signal in signals:
            remaining *= 1.0 - signal["severity"]
        return round(1.0 - remaining, 2)
