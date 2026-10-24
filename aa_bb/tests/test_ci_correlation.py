"""
Unit and integration tests for CI Correlation Engine, Baseline Analytics, and Historical Replay.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

# AA BigBrother
from aa_bb.counterintel.ingest.mumble import MumbleLogIngestor
from aa_bb.counterintel.ingest.paps import FCRosterIngestor
from aa_bb.counterintel.ingest.zkill import ZKillIngestor
from aa_bb.counterintel.models import (
    CICorrelation,
    CIOverride,
    CISession,
)
from aa_bb.counterintel.services.correlation import CorrelationEngine
from aa_bb.counterintel.services.identity import IdentityResolver
from aa_bb.counterintel.services.operations import CIOperationService
from aa_bb.counterintel.tasks import replay_historical_operation_task


class TestCICorrelation(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

        self.pilot_dps = User.objects.create_user(
            username="pilot_dps", email="dps@example.com"
        )
        self.pilot_logi = User.objects.create_user(
            username="pilot_logi", email="logi@example.com"
        )
        self.pilot_spy = User.objects.create_user(
            username="pilot_spy", email="spy@example.com"
        )
        self.pilot_habitual = User.objects.create_user(
            username="pilot_habitual", email="habit@example.com"
        )

        # Identity mappings
        IdentityResolver.register_identity(
            user=self.pilot_dps,
            character_id=2001,
            mumble_username="pilot_dps",
            effective_from=self.now - timedelta(days=65),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot_logi,
            character_id=2002,
            mumble_username="pilot_logi",
            effective_from=self.now - timedelta(days=65),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot_spy,
            character_id=2003,
            mumble_username="pilot_spy",
            effective_from=self.now - timedelta(days=65),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot_habitual,
            character_id=2004,
            mumble_username="pilot_habitual",
            effective_from=self.now - timedelta(days=65),
            is_verified=True,
        )

        # Seed historical baseline for pilot_habitual (habitually logs off at 21:00 UTC)
        for d in range(1, 30):
            t_start = (self.now - timedelta(days=d)).replace(
                hour=19, minute=0, second=0
            )
            t_end = (self.now - timedelta(days=d)).replace(hour=21, minute=0, second=0)
            CISession.objects.create(
                user=self.pilot_habitual,
                source="mumble",
                start_time=t_start,
                end_time=t_end,
                channel_name="General Comms",
            )

    def test_comms_without_pap_detection(self):
        op_start = self.now
        op = CIOperationService.create_operation(
            name="Op Strategic Push",
            start_time=op_start,
            end_time=op_start + timedelta(hours=2),
        )

        # FC credits only pilot_dps
        FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=[2001],
            recorded_at=op_start,
        )

        # pilot_spy was on comms for 45 minutes but has NO PAP
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            op_start + timedelta(minutes=5),
            channel_name="Fleet 1",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "disconnect",
            op_start + timedelta(minutes=50),
            channel_name="Fleet 1",
        )

        correlations = CorrelationEngine.correlate_operation(op)
        spy_corrs = [
            c
            for c in correlations
            if c.user == self.pilot_spy and c.anomaly_type == "comms_without_pap"
        ]

        self.assertEqual(len(spy_corrs), 1)
        self.assertGreaterEqual(spy_corrs[0].confidence, 0.70)
        self.assertIn("Fleet 1", spy_corrs[0].explanation)

    def test_pre_contact_disconnect_habitual_vs_outlier(self):
        # Operational scenario: Fleet battle at 21:10 UTC
        op_start = self.now.replace(hour=20, minute=0, second=0)
        op_end = self.now.replace(hour=22, minute=0, second=0)
        op = CIOperationService.create_operation(
            name="Op Ambush", start_time=op_start, end_time=op_end
        )

        # Combat starts at 21:10 UTC
        t_combat = op_start.replace(hour=21, minute=10, second=0)
        ZKillIngestor.ingest_killmail(
            {
                "killmail_id": 9999001,
                "killmail_time": t_combat.isoformat(),
                "solar_system_id": 30000142,
                "victim": {"character_id": 88888, "ship_type_id": 624},
                "attackers": [{"character_id": 2001, "damage_done": 1000}],
            },
            operation=op,
        )

        # pilot_habitual logs off at 21:02 UTC (matches daily 21:00 habit)
        t_disc_habit = op_start.replace(hour=21, minute=2, second=0)
        MumbleLogIngestor.ingest_raw_event(
            "pilot_habitual", "connect", op_start, channel_name="Op Comms"
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_habitual", "disconnect", t_disc_habit, channel_name="Op Comms"
        )

        # pilot_spy logs off at 21:02 UTC (outlier, no historical 21:00 habit)
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy", "connect", op_start, channel_name="Op Comms"
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy", "disconnect", t_disc_habit, channel_name="Op Comms"
        )

        correlations = CorrelationEngine.correlate_operation(op)

        habit_corr = next(
            (
                c
                for c in correlations
                if c.user == self.pilot_habitual
                and c.anomaly_type == "pre_contact_disconnect"
            ),
            None,
        )
        spy_corr = next(
            (
                c
                for c in correlations
                if c.user == self.pilot_spy
                and c.anomaly_type == "pre_contact_disconnect"
            ),
            None,
        )

        self.assertIsNotNone(habit_corr)
        self.assertIsNotNone(spy_corr)

        # Habitual disconnect should have low confidence (~0.25), Outlier should have high confidence (~0.85)
        self.assertLessEqual(habit_corr.confidence, 0.30)
        self.assertGreaterEqual(spy_corr.confidence, 0.80)
        self.assertIn("habitual", habit_corr.explanation)
        self.assertIn("outlier", spy_corr.explanation)

    def test_role_aware_false_positive_suppression(self):
        op = CIOperationService.create_operation(
            name="Op Logistics Run",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )

        # Tag objective with Recon and Logistics support roles
        CIOperationService.add_objective(
            operation=op,
            name="Forward Cyno & Remote Reps",
            objective_type="logi",
            expected_roles=["logistics", "recon", "cyno"],
        )

        # Credit pilot_logi on FC roster
        FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=[2002],
            recorded_at=self.now,
        )

        # Multiple killmails where pilot_logi is NOT on the killmail (expected for support)
        for k in range(5):
            ZKillIngestor.ingest_killmail(
                {
                    "killmail_id": 777000 + k,
                    "killmail_time": (self.now + timedelta(minutes=10 + k)).isoformat(),
                    "victim": {"character_id": 55555 + k},
                    "attackers": [{"character_id": 99999}],
                },
                operation=op,
            )

        correlations = CorrelationEngine.correlate_operation(op)
        absence_corrs = [
            c for c in correlations if c.anomaly_type == "combat_absence_unexplained"
        ]

        # Support roles must be suppressed from false accusations
        self.assertEqual(len(absence_corrs), 0)

    def test_ci_override_exemption(self):
        op = CIOperationService.create_operation(
            name="Op Reconnaissance",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )

        # Add CIOverride exempting pilot_spy from comms_without_pap (e.g. authorized off-grid scout)
        CIOverride.objects.create(
            user=self.pilot_spy,
            reason="Authorized cloaked observer on out-of-game relay",
            exempt_anomaly_types=["comms_without_pap"],
            created_by=self.pilot_dps,
        )

        # pilot_spy is on comms without PAP
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            self.now + timedelta(minutes=5),
            channel_name="Scout Comms",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "disconnect",
            self.now + timedelta(minutes=50),
            channel_name="Scout Comms",
        )

        correlations = CorrelationEngine.correlate_operation(op)
        spy_corrs = [
            c
            for c in correlations
            if c.user == self.pilot_spy and c.anomaly_type == "comms_without_pap"
        ]

        self.assertEqual(len(spy_corrs), 0)

    def test_historical_replay_task_reproducibility(self):
        op = CIOperationService.create_operation(
            name="Op Replay Target",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            self.now + timedelta(minutes=5),
            channel_name="Fleet",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "disconnect",
            self.now + timedelta(minutes=45),
            channel_name="Fleet",
        )

        result = replay_historical_operation_task(op.id, algorithm_version="1.0.0")

        self.assertEqual(result["operation_id"], op.id)
        self.assertEqual(result["findings_count"], 1)
        self.assertEqual(result["algorithm_version"], "1.0.0")
        self.assertEqual(CICorrelation.objects.filter(operation=op).count(), 1)
