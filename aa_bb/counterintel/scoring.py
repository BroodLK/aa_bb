"""
Counterintel scoring values: defaults and the live, admin-editable settings.

Every weight, confidence and threshold the detectors use is read through
``scoring()``. Officers tune them in Django admin (Counterintel scoring); the
values below are the defaults until a settings row exists.
"""

# Django
from django.core.cache import cache
from django.db import DatabaseError

SCORING_CACHE_KEY = "ci:scoring"
SCORING_CACHE_SECONDS = 60

# Findings older than this no longer count in the risk matrix; saving the
# scoring settings re-scores operations inside this window
RISK_MATRIX_LOOKBACK_DAYS = 30

DEFAULTS = {
    # Risk score factor weights (must total 1.0)
    "weight_comms": 0.25,
    "weight_discord": 0.10,
    "weight_alt": 0.20,
    "weight_deviation": 0.10,
    "weight_bb_signals": 0.20,
    "weight_leak": 0.10,
    "weight_accountability": 0.05,
    # Fixed confidence per finding type
    "conf_pre_contact_habitual": 0.25,
    "conf_pre_contact_outlier": 0.85,
    "conf_pre_contact_no_history": 0.45,
    "conf_briefing": 0.70,
    "conf_combat_absence": 0.45,
    "conf_alt_proximity": 0.85,
    "conf_alt_hostile": 0.90,
    "conf_shared_mumble": 0.85,
    "conf_disconnect_ring": 0.88,
    "conf_uncredited_alt": 0.40,
    "conf_eve_logoff": 0.45,
    "conf_unaudited": 0.35,
    # Bounds of findings whose confidence is calculated
    "comms_base": 0.40,
    "comms_max": 0.95,
    "comms_no_channel_cap": 0.60,
    "bb_sensitive_base": 0.40,
    "bb_sensitive_scale": 0.50,
    "leak_base": 0.40,
    "leak_max": 0.90,
    # Alerts and triage
    "alert_confidence": 0.85,
    "alert_risk": 0.80,
    "auto_triage_confidence": 0.50,
    # Leak cross-check
    "leak_min_ops": 2,
    "leak_min_rate": 0.5,
    "leak_min_lift": 2.0,
    # Secrecy multipliers (standard is always 1.0)
    "secrecy_restricted": 1.2,
    "secrecy_classified": 1.4,
    "secrecy_top_secret": 1.6,
    # Big Brother flag severities
    "sev_alliance_blacklist": 1.0,
    "sev_coalition_blacklist": 1.0,
    "sev_awox": 0.9,
    "sev_hostile_clones": 0.7,
    "sev_sus_contacts": 0.6,
    "sev_sus_contracts": 0.6,
    "sev_sus_mails": 0.6,
    "sev_hostile_assets": 0.5,
    "sev_sus_trans": 0.5,
    # Case dispositions
    "override_days": 14,
}

# Risk factor key -> settings field
RISK_WEIGHT_FIELDS = {
    "comms": "weight_comms",
    "discord": "weight_discord",
    "alt": "weight_alt",
    "deviation": "weight_deviation",
    "bb_signals": "weight_bb_signals",
    "leak": "weight_leak",
    "accountability": "weight_accountability",
}

# Finding type -> settings field, for findings with a fixed confidence
CONFIDENCE_FIELDS = {
    "briefing_without_deployment": "conf_briefing",
    "combat_absence_unexplained": "conf_combat_absence",
    "alt_operational_proximity": "conf_alt_proximity",
    "alt_in_hostile_entity": "conf_alt_hostile",
    "alt_shared_mumble_identity": "conf_shared_mumble",
    "co_occurring_disconnect_ring": "conf_disconnect_ring",
    "uncredited_alt_online": "conf_uncredited_alt",
    "eve_logoff_before_contact": "conf_eve_logoff",
    "unaudited_fleet_character": "conf_unaudited",
}

# Big Brother UserStatus flag -> settings field
FLAG_SEVERITY_FIELDS = {
    "has_alliance_blacklist": "sev_alliance_blacklist",
    "has_coalition_blacklist": "sev_coalition_blacklist",
    "has_awox_kills": "sev_awox",
    "has_hostile_clones": "sev_hostile_clones",
    "has_sus_contacts": "sev_sus_contacts",
    "has_sus_contracts": "sev_sus_contracts",
    "has_sus_mails": "sev_sus_mails",
    "has_hostile_assets": "sev_hostile_assets",
    "has_sus_trans": "sev_sus_trans",
}

# Secrecy level -> settings field
SECRECY_FIELDS = {
    "restricted": "secrecy_restricted",
    "classified": "secrecy_classified",
    "top_secret": "secrecy_top_secret",
}


class ScoringValues:
    """Read-only snapshot of the scoring settings with lookup helpers."""

    def __init__(self, values: dict):
        self._values = {**DEFAULTS, **values}
        for name, value in self._values.items():
            setattr(self, name, value)

    def risk_weights(self) -> dict:
        return {key: self._values[field] for key, field in RISK_WEIGHT_FIELDS.items()}

    def confidence(self, anomaly_type: str) -> float:
        return self._values[CONFIDENCE_FIELDS[anomaly_type]]

    def flag_severity(self, flag: str) -> float:
        return self._values[FLAG_SEVERITY_FIELDS[flag]]

    def secrecy_multiplier(self, level) -> float:
        field = SECRECY_FIELDS.get(str(level).lower())
        if field:
            return self._values[field]
        return 1.0


def scoring() -> ScoringValues:
    """
    The scoring settings in force, cached briefly. Falls back to the defaults
    when no settings row exists yet or the table is not migrated.
    """
    values = cache.get(SCORING_CACHE_KEY)
    if values is None:
        values = {}
        try:
            # AA BigBrother
            from aa_bb.counterintel.models import CIScoringConfig

            row = CIScoringConfig.objects.filter(pk=1).values(*DEFAULTS).first()
            values = row or {}
        except DatabaseError:
            values = {}
        cache.set(SCORING_CACHE_KEY, values, timeout=SCORING_CACHE_SECONDS)
    return ScoringValues(values)


def invalidate_scoring_cache():
    cache.delete(SCORING_CACHE_KEY)
