"""
Unit and integration tests for CI Correlation Engine, Baseline Analytics, and Historical Replay.
"""

# Standard Library
from datetime import timedelta
from unittest.mock import patch

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
    CIInvestigation,
    CIOverride,
    CISession,
)
from aa_bb.counterintel.scoring import scoring
from aa_bb.counterintel.services.correlation import CorrelationEngine
from aa_bb.counterintel.services.identity import IdentityResolver
from aa_bb.counterintel.services.operations import CIOperationService
from aa_bb.counterintel.tasks import (
    replay_historical_operation_task,
    run_scheduled_ci_correlation,
)


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

    def test_unaudited_fleet_character_flagged(self):
        # Third Party
        from corptools.models import CharacterAudit

        # Alliance Auth
        from allianceauth.eveonline.models import EveCharacter

        op = CIOperationService.create_operation(
            name="Op Audit Sweep",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op, roster_entries=[2001, 2002], recorded_at=self.now
        )
        audited_char = EveCharacter.objects.create(
            character_id=2002,
            character_name="Audited Logi",
            corporation_id=1,
            corporation_name="Corp",
        )
        CharacterAudit.objects.create(character=audited_char)

        flagged = {
            c.user
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "unaudited_fleet_character"
        }
        self.assertEqual(flagged, {self.pilot_dps})

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

        # pilot_spy habitually logs off at 03:00 UTC, so 21:02 is an outlier
        for d in range(1, 30):
            day = self.now - timedelta(days=d)
            CISession.objects.create(
                user=self.pilot_spy,
                source="mumble",
                start_time=day.replace(hour=1, minute=0, second=0),
                end_time=day.replace(hour=3, minute=0, second=0),
                channel_name="General Comms",
            )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy", "connect", op_start, channel_name="Op Comms"
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy", "disconnect", t_disc_habit, channel_name="Op Comms"
        )

        # pilot_logi has no comms history to judge the disconnect against
        MumbleLogIngestor.ingest_raw_event(
            "pilot_logi", "connect", op_start, channel_name="Op Comms"
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_logi", "disconnect", t_disc_habit, channel_name="Op Comms"
        )

        with patch.object(
            CorrelationEngine, "notify_high_confidence_finding"
        ) as mock_notify:
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

        # Without history the disconnect is reported, but stays below alerting
        new_corr = next(
            c
            for c in correlations
            if c.user == self.pilot_logi and c.anomaly_type == "pre_contact_disconnect"
        )
        self.assertLess(new_corr.confidence, scoring().alert_confidence)
        self.assertIn("not enough comms history", new_corr.explanation)
        # Only the disconnect finding matters here (they are also off the roster)
        notified_users = {
            call.args[0].user
            for call in mock_notify.call_args_list
            if call.args[0].anomaly_type == "pre_contact_disconnect"
        }
        self.assertNotIn(self.pilot_logi, notified_users)
        self.assertIn(self.pilot_spy, notified_users)

    def test_combat_absence_uses_every_killmail_participant(self):
        op = CIOperationService.create_operation(
            name="Op Brawl Fleet",
            start_time=self.now - timedelta(hours=2),
            end_time=self.now - timedelta(hours=1),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=[2001, 2002, 2003],
            recorded_at=op.start_time,
        )
        # pilot_dps and pilot_logi are both attackers on every kill; pilot_spy is on none
        for idx in range(3):
            ZKillIngestor.ingest_killmail(
                {
                    "killmail_id": 7100 + idx,
                    "killmail_time": (
                        op.start_time + timedelta(minutes=10 + idx)
                    ).isoformat(),
                    "victim": {"character_id": 77000 + idx},
                    "attackers": [{"character_id": 2001}, {"character_id": 2002}],
                },
                operation=op,
            )

        absent = {
            c.user
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "combat_absence_unexplained"
        }
        self.assertEqual(absent, {self.pilot_spy})

    def test_briefing_detector_covers_top_secret_operations(self):
        op = CIOperationService.create_operation(
            name="Op Top Secret Brief",
            start_time=self.now - timedelta(days=9),
            end_time=self.now - timedelta(days=9) + timedelta(minutes=30),
            secrecy_level="top_secret",
        )
        CISession.objects.create(
            user=self.pilot_spy,
            source="mumble",
            start_time=op.start_time,
            end_time=op.end_time,
            channel_name="Briefing",
        )
        flagged = {
            c.user
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "briefing_without_deployment"
        }
        self.assertIn(self.pilot_spy, flagged)

    def test_briefing_deployment_only_judged_after_window(self):
        op = CIOperationService.create_operation(
            name="Op Classified Brief",
            start_time=self.now - timedelta(hours=1),
            end_time=self.now - timedelta(minutes=30),
            secrecy_level="classified",
        )
        CISession.objects.create(
            user=self.pilot_spy,
            source="mumble",
            start_time=op.start_time,
            end_time=op.end_time,
            channel_name="Briefing",
        )
        anomalies = {c.anomaly_type for c in CorrelationEngine.correlate_operation(op)}
        self.assertNotIn("briefing_without_deployment", anomalies)

        # Once the deployment window has passed, the non-deploying attendee is flagged
        old_op = CIOperationService.create_operation(
            name="Op Old Brief",
            start_time=self.now - timedelta(days=9),
            end_time=self.now - timedelta(days=9) + timedelta(minutes=30),
            secrecy_level="classified",
        )
        CISession.objects.create(
            user=self.pilot_spy,
            source="mumble",
            start_time=old_op.start_time,
            end_time=old_op.end_time,
            channel_name="Briefing",
        )
        flagged = {
            c.user
            for c in CorrelationEngine.correlate_operation(old_op)
            if c.anomaly_type == "briefing_without_deployment"
        }
        self.assertIn(self.pilot_spy, flagged)

    def test_unaudited_character_does_not_open_case_alone(self):
        # AA BigBrother
        from aa_bb.counterintel.services.agent import CIAgentService

        op = CIOperationService.create_operation(
            name="Op Unaudited",
            start_time=self.now - timedelta(hours=3),
            end_time=self.now - timedelta(hours=2),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op, roster_entries=[2001], recorded_at=op.start_time
        )
        cases = CIAgentService.auto_triage_operation(op)
        self.assertTrue(
            CICorrelation.objects.filter(
                operation=op, anomaly_type="unaudited_fleet_character"
            ).exists()
        )
        self.assertFalse([c for c in cases if c.subject_user == self.pilot_dps])

    def test_pipeline_skips_when_already_running(self):
        # Django
        from django.core.cache import cache

        # AA BigBrother
        from aa_bb.counterintel import tasks as ci_tasks

        cache.set(ci_tasks.PIPELINE_LOCK_KEY, 1, timeout=60)
        try:
            with patch.object(ci_tasks, "sync_recent_afat_fleets_task") as mock_sync:
                self.assertIsNone(ci_tasks.run_counterintel_pipeline_task())
            mock_sync.assert_not_called()
        finally:
            cache.delete(ci_tasks.PIPELINE_LOCK_KEY)

    def _eve_activity(self, user, character_id, activity_type, timestamp):
        # AA BigBrother
        from aa_bb.counterintel.models import CIEvent

        payload = {
            "character_id": character_id,
            "character_name": f"Char {character_id}",
            "activity_type": activity_type,
        }
        source_event_id = f"eve:{activity_type}:{character_id}:{timestamp.isoformat()}"
        return CIEvent.objects.create(
            source="eve_activity",
            source_event_id=source_event_id,
            timestamp=timestamp,
            user=user,
            payload=payload,
            dedup_hash=CIEvent.generate_dedup_hash(
                "eve_activity", source_event_id, timestamp, payload
            ),
        )

    # ---- Admin-editable scoring settings ----

    def _save_scoring(self, **values):
        # AA BigBrother
        from aa_bb.counterintel.models import CIScoringConfig
        from aa_bb.counterintel.scoring import invalidate_scoring_cache

        # The cache outlives the test transaction; never leak values into other tests
        self.addCleanup(invalidate_scoring_cache)
        config = CIScoringConfig.get_solo()
        for name, value in values.items():
            setattr(config, name, value)
        with patch(
            "aa_bb.counterintel.tasks.rescore_recent_operations_task.delay"
        ) as mock_delay:
            with self.captureOnCommitCallbacks(execute=True):
                config.save()
        return config, mock_delay

    def test_scoring_defaults_without_settings_row(self):
        # AA BigBrother
        from aa_bb.counterintel.scoring import DEFAULTS, invalidate_scoring_cache

        invalidate_scoring_cache()
        cfg = scoring()
        self.assertEqual(cfg.alert_confidence, DEFAULTS["alert_confidence"])
        self.assertEqual(cfg.confidence("uncredited_alt_online"), 0.40)
        self.assertAlmostEqual(sum(cfg.risk_weights().values()), 1.0)
        self.assertEqual(cfg.secrecy_multiplier("standard"), 1.0)

    def test_changed_confidence_applies_and_queues_rescore(self):
        _config, mock_delay = self._save_scoring(conf_uncredited_alt=0.33)
        mock_delay.assert_called_once_with()

        op = CIOperationService.create_operation(
            name="Op Tuned",
            start_time=self.now - timedelta(hours=2),
            end_time=self.now - timedelta(hours=1),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op, roster_entries=[2001], recorded_at=op.start_time
        )
        self._eve_activity(
            self.pilot_dps, 2999, "login", op.start_time + timedelta(minutes=20)
        )
        found = [
            c
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "uncredited_alt_online"
        ]
        self.assertEqual([c.confidence for c in found], [0.33])

    def test_scoring_validation(self):
        # Django
        from django.core.exceptions import ValidationError

        # AA BigBrother
        from aa_bb.counterintel.models import CIScoringConfig

        config = CIScoringConfig(weight_comms=0.15)  # weights now total 90%
        with self.assertRaises(ValidationError) as ctx:
            config.clean()
        self.assertIn("90.0", str(ctx.exception))

        config = CIScoringConfig(secrecy_classified=1.1)  # below Restricted (1.2)
        with self.assertRaises(ValidationError):
            config.clean()

        config = CIScoringConfig(alert_confidence=0.4)  # below case opening (0.5)
        with self.assertRaises(ValidationError):
            config.clean()

        CIScoringConfig().clean()  # defaults are valid

    def test_uncredited_alt_login_during_operation(self):
        op = CIOperationService.create_operation(
            name="Op Alt Watch",
            start_time=self.now - timedelta(hours=2),
            end_time=self.now - timedelta(hours=1),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op, roster_entries=[2001, 2002], recorded_at=op.start_time
        )
        # pilot_dps logs in an alt that is not on the roster
        self._eve_activity(
            self.pilot_dps, 2999, "login", op.start_time + timedelta(minutes=20)
        )
        # pilot_logi only logs in the credited fleet character
        self._eve_activity(
            self.pilot_logi, 2002, "login", op.start_time + timedelta(minutes=5)
        )

        found = {
            c.user: c
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "uncredited_alt_online"
        }
        self.assertEqual(set(found), {self.pilot_dps})
        self.assertEqual(found[self.pilot_dps].confidence, 0.40)
        self.assertIn("Char 2999", found[self.pilot_dps].explanation)

    def test_eve_logoff_shortly_before_contact(self):
        op = CIOperationService.create_operation(
            name="Op Logoff Watch",
            start_time=self.now - timedelta(hours=2),
            end_time=self.now - timedelta(hours=1),
        )
        FCRosterIngestor.ingest_fc_roster(
            operation=op, roster_entries=[2001, 2002], recorded_at=op.start_time
        )
        first_contact = op.start_time + timedelta(minutes=40)
        ZKillIngestor.ingest_killmail(
            {
                "killmail_id": 7301,
                "killmail_time": first_contact.isoformat(),
                "victim": {"character_id": 77301},
                "attackers": [{"character_id": 2002}],
            },
            operation=op,
        )
        self._eve_activity(
            self.pilot_dps, 2001, "logoff", first_contact - timedelta(minutes=5)
        )
        # Logging off well before contact is not flagged
        self._eve_activity(
            self.pilot_logi, 2002, "logoff", first_contact - timedelta(minutes=35)
        )

        flagged = {
            c.user
            for c in CorrelationEngine.correlate_operation(op)
            if c.anomaly_type == "eve_logoff_before_contact"
        }
        self.assertEqual(flagged, {self.pilot_dps})

    def test_bb_flags_reported_for_sensitive_operations_only(self):
        # AA BigBrother
        from aa_bb.models import UserStatus

        UserStatus.objects.update_or_create(
            user=self.pilot_dps, defaults={"has_hostile_assets": True}
        )
        sensitive = CIOperationService.create_operation(
            name="Op Classified Strike",
            start_time=self.now - timedelta(hours=2),
            end_time=self.now - timedelta(hours=1),
            secrecy_level="classified",
        )
        standard = CIOperationService.create_operation(
            name="Op Casual Roam",
            start_time=self.now - timedelta(hours=5),
            end_time=self.now - timedelta(hours=4),
        )
        for op in (sensitive, standard):
            FCRosterIngestor.ingest_fc_roster(
                operation=op, roster_entries=[2001], recorded_at=op.start_time
            )

        sensitive_corr = [
            c
            for c in CorrelationEngine.correlate_operation(sensitive)
            if c.anomaly_type == "bb_flagged_in_sensitive_op"
        ]
        self.assertEqual(len(sensitive_corr), 1)
        self.assertEqual(sensitive_corr[0].user, self.pilot_dps)
        self.assertAlmostEqual(sensitive_corr[0].confidence, 0.65)
        self.assertIn("Hostile assets", sensitive_corr[0].explanation)

        standard_corr = CorrelationEngine.correlate_operation(standard)
        self.assertFalse(
            [c for c in standard_corr if c.anomaly_type == "bb_flagged_in_sensitive_op"]
        )
        # The flag still raises the member's risk on any operation
        self.assertTrue(
            all(
                c.factors["bb_signals"] == 0.5
                for c in standard_corr
                if c.user == self.pilot_dps
            )
        )

    def _concluded_op(self, name, days_ago, leaked=False, roster_users=()):
        # AA BigBrother
        from aa_bb.counterintel.models import CIOperationParticipant

        start = self.now - timedelta(days=days_ago)
        op = CIOperationService.create_operation(
            name=name, start_time=start, end_time=start + timedelta(hours=1)
        )
        if leaked:
            op.leaked = True
            op.save(update_fields=["leaked"])
        for idx, user in enumerate(roster_users):
            CIOperationParticipant.objects.create(
                operation=op,
                user=user,
                character_id=500000 + user.id * 100 + idx,
                confirmed_by_fc=True,
                recorded_at=start,
            )
        return op

    def test_leak_exposure_flags_disproportionate_attendance(self):
        # Django
        from django.core.cache import cache

        cache.clear()
        # pilot_spy knew about every leaked op and no clean ones; pilot_dps
        # attends everything, so their leak attendance is unremarkable
        leaked = [
            self._concluded_op(
                f"Leaked {i}",
                10 + i,
                leaked=True,
                roster_users=(self.pilot_spy, self.pilot_dps),
            )
            for i in range(3)
        ]
        for i in range(5):
            self._concluded_op(f"Clean {i}", 20 + i, roster_users=(self.pilot_dps,))

        found = {
            c.user: c
            for c in CorrelationEngine.correlate_operation(leaked[0])
            if c.anomaly_type == "leak_exposure"
        }
        self.assertEqual(set(found), {self.pilot_spy})
        self.assertIn("3 of 3", found[self.pilot_spy].explanation)
        self.assertGreaterEqual(found[self.pilot_spy].confidence, 0.5)

    def test_single_leak_flags_nobody(self):
        # Django
        from django.core.cache import cache

        cache.clear()
        leaked = self._concluded_op(
            "Only Leak", 5, leaked=True, roster_users=(self.pilot_spy,)
        )
        self._concluded_op("Clean A", 6, roster_users=(self.pilot_dps,))
        self.assertFalse(
            [
                c
                for c in CorrelationEngine.correlate_operation(leaked)
                if c.anomaly_type == "leak_exposure"
            ]
        )

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

        # Once the override expires, the detector flags them again
        CIOverride.objects.filter(user=self.pilot_spy).update(
            expires_at=self.now - timedelta(minutes=1)
        )
        correlations = CorrelationEngine.correlate_operation(op)
        self.assertTrue(
            [
                c
                for c in correlations
                if c.user == self.pilot_spy and c.anomaly_type == "comms_without_pap"
            ]
        )

    def test_exposure_cache_invalidation(self):
        # Django
        from django.core.cache import cache

        cache.clear()
        op = self._concluded_op("Cached Op", 3)
        self.assertNotIn(self.pilot_logi.id, CorrelationEngine._cached_exposure(op))

        CISession.objects.create(
            user=self.pilot_logi,
            source="mumble",
            start_time=op.start_time - timedelta(minutes=10),
            end_time=op.start_time + timedelta(minutes=30),
        )
        # Still the cached set until invalidated
        self.assertNotIn(self.pilot_logi.id, CorrelationEngine._cached_exposure(op))
        CorrelationEngine.invalidate_exposure_cache()
        self.assertIn(self.pilot_logi.id, CorrelationEngine._cached_exposure(op))

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

    def _spy_on_comms(self, op_start, channel_name="Fleet 1"):
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            op_start + timedelta(minutes=5),
            channel_name=channel_name,
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "disconnect",
            op_start + timedelta(minutes=50),
            channel_name=channel_name,
        )

    def test_comms_without_pap_capped_without_channel_data(self):
        op = CIOperationService.create_operation(
            name="Op No Channels",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )
        self._spy_on_comms(self.now, channel_name="")

        corr = CorrelationEngine.correlate_operation(op)
        spy = [c for c in corr if c.user == self.pilot_spy]
        self.assertEqual(len(spy), 1)
        self.assertLessEqual(spy[0].confidence, scoring().comms_no_channel_cap)

    def test_idle_all_day_presence_not_flagged(self):
        op = CIOperationService.create_operation(
            name="Op Idle Check",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )
        # Connected six hours before the op and never left
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy", "connect", self.now - timedelta(hours=6)
        )

        corr = CorrelationEngine.correlate_operation(op)
        self.assertFalse([c for c in corr if c.anomaly_type == "comms_without_pap"])

    def test_stale_correlations_pruned_unless_investigated(self):
        op = CIOperationService.create_operation(
            name="Op Prune",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )
        self._spy_on_comms(self.now)
        CorrelationEngine.correlate_operation(op)
        self.assertEqual(CICorrelation.objects.filter(operation=op).count(), 1)

        # Spy's sessions turn out to be bad data; the finding should disappear
        CISession.objects.filter(user=self.pilot_spy).delete()
        CorrelationEngine.correlate_operation(op)
        self.assertEqual(CICorrelation.objects.filter(operation=op).count(), 0)

        # A finding backing an investigation is preserved
        MumbleLogIngestor.rebuild_user_sessions(self.pilot_spy)
        CorrelationEngine.correlate_operation(op)
        self.assertEqual(CICorrelation.objects.filter(operation=op).count(), 1)
        CIInvestigation.objects.create(subject_user=self.pilot_spy, operation=op)
        CISession.objects.filter(user=self.pilot_spy).delete()
        CorrelationEngine.correlate_operation(op)
        self.assertEqual(CICorrelation.objects.filter(operation=op).count(), 1)

    @patch.object(CorrelationEngine, "notify_high_confidence_finding")
    def test_high_confidence_alert_sent_once(self, mock_notify):
        op = CIOperationService.create_operation(
            name="Op Alert Once",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )
        # 100 minutes on a named fleet channel -> confidence above the alert threshold
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            self.now + timedelta(minutes=5),
            channel_name="Fleet 1",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "disconnect",
            self.now + timedelta(minutes=110),
            channel_name="Fleet 1",
        )

        CorrelationEngine.correlate_operation(op)
        CorrelationEngine.correlate_operation(op)
        self.assertEqual(mock_notify.call_count, 1)

    def test_scheduled_correlation_triages_concluded_operations(self):
        op = CIOperationService.create_operation(
            name="Op Concluded",
            start_time=self.now - timedelta(hours=4),
            end_time=self.now - timedelta(hours=2),
        )
        self._spy_on_comms(op.start_time)

        run_scheduled_ci_correlation(hours=48)
        self.assertTrue(
            CIInvestigation.objects.filter(
                subject_user=self.pilot_spy, operation=op, status="pending"
            ).exists()
        )

        # A case the reviewer closed is not reopened or rewritten
        CIInvestigation.objects.filter(operation=op).update(
            status="cleared", agent_summary="reviewed"
        )
        run_scheduled_ci_correlation(hours=48)
        case = CIInvestigation.objects.get(subject_user=self.pilot_spy, operation=op)
        self.assertEqual((case.status, case.agent_summary), ("cleared", "reviewed"))
