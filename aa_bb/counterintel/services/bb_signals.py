"""
Bridge from Big Brother's per-member checks (UserStatus) into counterintel scoring.
"""

# AA BigBrother
from aa_bb.models import UserStatus

from ..scoring import scoring


class BBSignals:
    """
    Reads the cached Big Brother flags for members and weights the ones that
    indicate hostile ties. Readiness checks (cyno, skills) are not spy indicators.
    """

    # UserStatus flag -> label; severities (0..1) are scoring settings
    FLAG_LABELS = {
        "has_alliance_blacklist": "Alliance blacklist",
        "has_coalition_blacklist": "Coalition blacklist",
        "has_awox_kills": "Awox kills",
        "has_hostile_clones": "Hostile clones",
        "has_sus_contacts": "Suspicious contacts",
        "has_sus_contracts": "Suspicious contracts",
        "has_sus_mails": "Suspicious mails",
        "has_hostile_assets": "Hostile assets",
        "has_sus_trans": "Suspicious transactions",
    }

    @classmethod
    def flag_severities(cls) -> list:
        """[(label, severity), ...] in display order, from the live settings."""
        cfg = scoring()
        return [
            (label, cfg.flag_severity(flag)) for flag, label in cls.FLAG_LABELS.items()
        ]

    @classmethod
    def for_users(cls, user_ids) -> dict:
        """{user_id: [{"flag", "label", "severity"}, ...]} for flagged users."""
        user_ids = [uid for uid in user_ids if uid]
        if not user_ids:
            return {}
        cfg = scoring()
        rows = UserStatus.objects.filter(user_id__in=user_ids).values(
            "user_id", *cls.FLAG_LABELS
        )
        signals = {}
        for row in rows:
            flags = [
                {"flag": flag, "label": label, "severity": cfg.flag_severity(flag)}
                for flag, label in cls.FLAG_LABELS.items()
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
