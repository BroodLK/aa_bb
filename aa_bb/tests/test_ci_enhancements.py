"""
Unit and integration tests for Counterintelligence enhancements across all 6 core pillars.
"""

# Standard Library
import os
import tempfile
from datetime import timedelta

# Django
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership, State, UserProfile
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.counterintel.ingest.mumble import MumbleLogIngestor
from aa_bb.counterintel.ingest.zkill import ZKillIngestor
from aa_bb.counterintel.models import (
    CICorrelation,
    CIEvent,
    CIIdentity,
    CIInvestigation,
    CILogIngestState,
    CIOperation,
    CIOperationParticipant,
    CIOverride,
    CISession,
)
from aa_bb.counterintel.scoring import scoring
from aa_bb.counterintel.services.agent import CIAgentService
from aa_bb.counterintel.services.baseline import BaselineEngine
from aa_bb.counterintel.services.correlation import CorrelationEngine
from aa_bb.counterintel.tasks import prune_ci_telemetry_task
from aa_bb.models import BigBrotherConfig, General, UserStatus


class CIEnhancementsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        cls.member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        cls.guest_state, _ = State.objects.get_or_create(
            name="Guest", defaults={"priority": 50}
        )

        cfg = BigBrotherConfig.get_solo()
        cfg.bb_member_states.set([cls.member_state])
        cfg.hostile_corporations = "99001122"
        cfg.hostile_alliances = "99003344"
        cfg.save()

        # Users
        cls.user1 = User.objects.create_user(
            username="pilot_alpha", password="password"
        )
        cls.user2 = User.objects.create_user(
            username="pilot_bravo", password="password"
        )
        cls.reviewer = User.objects.create_user(
            username="ci_auditor", password="password"
        )

        # Assign permissions
        content_type = ContentType.objects.get_for_model(General)
        perm_view, _ = Permission.objects.get_or_create(
            codename="can_view_ci",
            content_type=content_type,
            defaults={"name": "Can view Counterintelligence information"},
        )
        perm_review, _ = Permission.objects.get_or_create(
            codename="can_review_ci",
            content_type=content_type,
            defaults={"name": "Can review Counterintelligence investigations"},
        )
        cls.reviewer.user_permissions.add(perm_view, perm_review)

        # Profiles
        cls.char1 = EveCharacter.objects.create(
            character_id=1001,
            character_name="Pilot Alpha Main",
            corporation_id=2001,
            corporation_name="Main Corp",
            alliance_id=3001,
            alliance_name="Main Alliance",
        )
        CharacterOwnership.objects.create(
            character=cls.char1, user=cls.user1, owner_hash="hash_alpha"
        )
        cls.user1.profile.main_character = cls.char1
        cls.user1.profile.state = cls.member_state
        cls.user1.profile.save()
        UserProfile.objects.filter(pk=cls.user1.profile.pk).update(
            state=cls.member_state
        )

        UserStatus.objects.create(
            user=cls.user1,
            last_discord_message_at=timezone.now() - timedelta(days=2),
        )

        cls.char2 = EveCharacter.objects.create(
            character_id=1002,
            character_name="Pilot Bravo Main",
            corporation_id=2001,
            corporation_name="Main Corp",
            alliance_id=3001,
            alliance_name="Main Alliance",
        )
        CharacterOwnership.objects.create(
            character=cls.char2, user=cls.user2, owner_hash="hash_bravo"
        )
        cls.user2.profile.main_character = cls.char2
        cls.user2.profile.state = cls.member_state
        cls.user2.profile.save()
        UserProfile.objects.filter(pk=cls.user2.profile.pk).update(
            state=cls.member_state
        )

        cls.char_rev = EveCharacter.objects.create(
            character_id=1003,
            character_name="Auditor Main",
            corporation_id=2001,
            corporation_name="Main Corp",
            alliance_id=3001,
            alliance_name="Main Alliance",
        )
        CharacterOwnership.objects.create(
            character=cls.char_rev, user=cls.reviewer, owner_hash="hash_rev"
        )
        cls.reviewer.profile.main_character = cls.char_rev
        cls.reviewer.profile.state = cls.member_state
        cls.reviewer.profile.save()
        UserProfile.objects.filter(pk=cls.reviewer.profile.pk).update(
            state=cls.member_state
        )

        CIIdentity.objects.create(
            user=cls.user1, character_id=1001, mumble_username="pilot_alpha"
        )
        CIIdentity.objects.create(
            user=cls.user2, character_id=1002, mumble_username="pilot_bravo"
        )

    def setUp(self):
        cache.clear()

    # -------------------------------------------------------------
    # 1. Multi-Character & Alt-Identity Correlation Tests
    # -------------------------------------------------------------
    def test_alt_in_hostile_entity_detection(self):
        """Verify detection when user holds an alt in a configured hostile entity."""
        hostile_char = EveCharacter.objects.create(
            character_id=9001,
            character_name="Secret Spy Alt",
            corporation_id=99001122,  # Configured hostile corp
            corporation_name="Hostile Spy Corp",
        )
        # Link alt character to user1
        self.user1.character_ownerships.create(character=hostile_char)

        now = timezone.now()
        op = CIOperation.objects.create(
            name="Strategic Fleet Alpha",
            start_time=now - timedelta(hours=2),
            end_time=now,
            secrecy_level="classified",
        )
        CIOperationParticipant.objects.create(
            operation=op, user=self.user1, character_id=1001
        )

        findings = CorrelationEngine.correlate_operation(op)
        hostile_finding = next(
            (
                f
                for f in findings
                if f.anomaly_type == "alt_in_hostile_entity" and f.user == self.user1
            ),
            None,
        )
        self.assertIsNotNone(hostile_finding)
        self.assertGreaterEqual(hostile_finding.confidence, 0.85)
        self.assertIn("Secret Spy Alt", hostile_finding.explanation)

    def test_alt_operational_proximity_detection(self):
        """Verify detection when an uncredited alt is active in the operational solar system."""
        alt_char = EveCharacter.objects.create(
            character_id=9002,
            character_name="Cyno Scout Alt",
            corporation_id=2001,
            corporation_name="Main Corp",
        )
        self.user1.character_ownerships.create(character=alt_char)

        now = timezone.now()
        op = CIOperation.objects.create(
            name="Op Proximity",
            start_time=now - timedelta(hours=2),
            end_time=now,
        )
        CIOperationParticipant.objects.create(
            operation=op, user=self.user1, character_id=1001
        )

        # Ingest combat event where alt was on grid in solar system 30000142
        ZKillIngestor.ingest_killmail(
            {
                "killmail_id": "9998881",
                "killmail_time": (now - timedelta(minutes=45)).isoformat(),
                "solar_system_id": 30000142,
                "victim": {"character_id": 9002},
            },
            operation=op,
        )

        findings = CorrelationEngine.correlate_operation(op)
        prox_finding = next(
            (
                f
                for f in findings
                if f.anomaly_type == "alt_operational_proximity"
                and f.user == self.user1
            ),
            None,
        )
        self.assertIsNotNone(prox_finding)
        self.assertIn("30000142", prox_finding.explanation)

    # -------------------------------------------------------------
    # 2. Ingest Scalability & Real-Time Telemetry Tests
    # -------------------------------------------------------------
    def test_mumble_log_ingest_state_seek_offsets(self):
        """Verify incremental seek-offset tracking during log parsing."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as tmp:
            tmp.write(
                "[24/Oct/2026 10:00:00] INFO [mumble_authenticator:599] "
                f"User authenticated: pilot_alpha {self.user1.pk + 1000000000}\n"
                "[24/Oct/2026 10:00:00] INFO [mumble_authenticator:600] "
                "Group memberships: ['Member']\n"
            )
            tmp.flush()
            tmp_path = tmp.name

        try:
            # First pass: ingests 1 line, records offset
            events_pass1 = MumbleLogIngestor.ingest_log_file(tmp_path, use_offsets=True)
            self.assertEqual(len(events_pass1), 1)

            state = CILogIngestState.objects.get(file_path=tmp_path)
            self.assertGreater(state.last_offset, 0)

            # Second pass with no new lines: returns 0 new events
            events_pass2 = MumbleLogIngestor.ingest_log_file(tmp_path, use_offsets=True)
            self.assertEqual(len(events_pass2), 0)

            # Append new line
            with open(tmp_path, "a", encoding="utf-8") as f:
                f.write(
                    "[24/Oct/2026 10:05:00] INFO [mumble_authenticator:607] "
                    f"Failed authentication attempt for user: pilot_alpha {self.user1.pk}\n"
                )

            events_pass3 = MumbleLogIngestor.ingest_log_file(tmp_path, use_offsets=True)
            self.assertEqual(len(events_pass3), 1)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_zkill_analysis_only_on_completed_operation(self):
        """Verify zKill analysis is strictly deferred until operation end_time is populated."""
        now = timezone.now()
        active_op = CIOperation.objects.create(
            name="Active In-Flight Op",
            start_time=now - timedelta(hours=1),
            end_time=None,  # Ongoing
        )
        res_active = ZKillIngestor.analyze_completed_operation(active_op)
        self.assertEqual(res_active, [])

        completed_op = CIOperation.objects.create(
            name="Concluded Fleet Op",
            start_time=now - timedelta(hours=3),
            end_time=now - timedelta(hours=1),
        )
        res_completed = ZKillIngestor.analyze_completed_operation(
            completed_op,
            killmails=[
                {
                    "killmail_id": "777001",
                    "killmail_time": (now - timedelta(hours=2)).isoformat(),
                    "solar_system_id": 30000142,
                    "victim": {"character_id": 1002},
                }
            ],
        )
        self.assertEqual(len(res_completed), 1)

    # -------------------------------------------------------------
    # 3. Statistical Baseline Modeling & Anomaly Weighting Tests
    # -------------------------------------------------------------
    def test_circadian_2d_grid_and_weekday_profiling(self):
        """Verify 2D circadian baseline grid calculation and caching."""
        now = timezone.now()
        CISession.objects.create(
            user=self.user1,
            source="mumble",
            start_time=now - timedelta(hours=1),
            end_time=now,
        )

        baseline = BaselineEngine.calculate_personal_baseline(
            self.user1, reference_time=now
        )
        self.assertIn("circadian_grid", baseline)
        self.assertEqual(len(baseline["circadian_grid"]), 7)
        self.assertEqual(len(baseline["circadian_grid"][0]), 24)

    def test_bayesian_user_risk_scoring(self):
        """Verify Bayesian risk scoring incorporates comms, discord inactivity, and secrecy."""
        risk_score, factors = BaselineEngine.calculate_user_risk_score(
            self.user1,
            findings=[
                {"anomaly_type": "pre_contact_disconnect", "confidence": 0.85},
                {"anomaly_type": "alt_in_hostile_entity", "confidence": 0.90},
            ],
        )
        self.assertGreater(risk_score, 0.40)
        self.assertIn("comms_disconnect_timing", factors)
        self.assertIn("alt_proximity_and_hostility", factors)
        for key in ("bb_signals", "leak_exposure", "audit_accountability"):
            self.assertIn(key, factors)

        # A Big Brother hostile-tie flag raises the same findings' risk
        # AA BigBrother
        from aa_bb.models import UserStatus

        UserStatus.objects.update_or_create(
            user=self.user1, defaults={"has_alliance_blacklist": True}
        )
        flagged_score, flagged_factors = BaselineEngine.calculate_user_risk_score(
            self.user1,
            findings=[
                {"anomaly_type": "pre_contact_disconnect", "confidence": 0.85},
                {"anomaly_type": "alt_in_hostile_entity", "confidence": 0.90},
            ],
        )
        self.assertEqual(flagged_factors["bb_signals"], 1.0)
        self.assertGreater(flagged_score, risk_score)

    def test_secrecy_levels_scale_risk_in_order(self):
        """Higher secrecy levels must never weigh less than lower ones."""
        # AA BigBrother
        from aa_bb.counterintel.models import CIOperation

        findings = [{"anomaly_type": "comms_without_pap", "confidence": 0.6}]
        scores = []
        for level in ("standard", "restricted", "classified", "top_secret"):
            op = CIOperation(name=level, start_time=timezone.now(), secrecy_level=level)
            score, factors = BaselineEngine.calculate_user_risk_score(
                self.user1, operation=op, findings=findings
            )
            scores.append((factors["secrecy_multiplier"], score))
        multipliers = [m for m, _score in scores]
        self.assertEqual(multipliers, sorted(multipliers))
        self.assertEqual(len(set(multipliers)), 4)
        self.assertFalse(CIOperation(secrecy_level="standard").is_sensitive)
        self.assertTrue(CIOperation(secrecy_level="top_secret").is_sensitive)
        # Unknown legacy values fall back to neutral
        self.assertEqual(CIOperation(secrecy_level="high").secrecy_multiplier, 1.0)

    def test_co_occurring_disconnect_ring_detection(self):
        """Verify detection of leak rings where multiple users drop comms together."""
        now = timezone.now()
        op1 = CIOperation.objects.create(
            name="Past Operation 1",
            start_time=now - timedelta(days=5),
            end_time=now - timedelta(days=5) + timedelta(hours=2),
        )
        CICorrelation.objects.create(
            operation=op1,
            user=self.user1,
            anomaly_type="pre_contact_disconnect",
            confidence=0.85,
        )
        CICorrelation.objects.create(
            operation=op1,
            user=self.user2,
            anomaly_type="pre_contact_disconnect",
            confidence=0.85,
        )

        op2 = CIOperation.objects.create(
            name="Current Operation 2",
            start_time=now - timedelta(hours=2),
            end_time=now,
        )
        combat_time = now - timedelta(minutes=20)
        ZKillIngestor.ingest_killmail(
            {
                "killmail_id": "888111",
                "killmail_time": combat_time.isoformat(),
                "solar_system_id": 30000142,
            },
            operation=op2,
        )

        drop_time = combat_time - timedelta(minutes=5)
        CISession.objects.create(
            user=self.user1,
            source="mumble",
            start_time=op2.start_time,
            end_time=drop_time,
        )
        CISession.objects.create(
            user=self.user2,
            source="mumble",
            start_time=op2.start_time,
            end_time=drop_time,
        )

        findings = CorrelationEngine.correlate_operation(op2)
        ring_findings = [
            f for f in findings if f.anomaly_type == "co_occurring_disconnect_ring"
        ]
        self.assertGreaterEqual(len(ring_findings), 2)

    # -------------------------------------------------------------
    # 4. Investigation Lifecycle & Chain of Custody Tests
    # -------------------------------------------------------------
    def test_case_dossier_aggregation_and_cryptographic_hash(self):
        """Verify aggregated case dossier contains SHA-256 integrity hash."""
        dossier = CIAgentService.generate_case_dossier(self.user1)
        self.assertIn("integrity_hash", dossier)
        self.assertEqual(len(dossier["integrity_hash"]), 64)
        self.assertEqual(dossier["subject_username"], "pilot_alpha")

    def test_false_positive_feedback_loop_and_override_creation(self):
        """Verify resolution tag in manual review creates scoped CIOverride."""
        now = timezone.now()
        op = CIOperation.objects.create(
            name="Op Investigation",
            start_time=now - timedelta(hours=2),
            end_time=now,
        )
        CICorrelation.objects.create(
            operation=op,
            user=self.user1,
            anomaly_type="comms_without_pap",
            confidence=0.75,
        )
        # Hostile ties are never exempted by a case disposition
        CICorrelation.objects.create(
            operation=op,
            user=self.user1,
            anomaly_type="alt_in_hostile_entity",
            confidence=0.90,
        )
        inv = CIInvestigation.objects.create(
            operation=op, subject_user=self.user1, status="pending"
        )

        self.client.force_login(self.reviewer)
        url = reverse("ci:investigation_detail", args=[inv.id])
        # The form states the real exemption length
        self.assertContains(self.client.get(url), f"for {scoring().override_days} days")
        response = self.client.post(
            url,
            {
                "status": "cleared",
                "resolution_tag": "scout_alt",
                "reviewer_notes": "Subject was authorized covert scout on voice.",
            },
        )
        self.assertEqual(response.status_code, 302)

        inv.refresh_from_db()
        self.assertEqual(inv.status, "cleared")
        self.assertEqual(inv.resolution_tag, "scout_alt")
        self.assertTrue(bool(inv.dossier_hash))

        override = CIOverride.objects.filter(user=self.user1).first()
        self.assertIsNotNone(override)
        self.assertIn("comms_without_pap", override.exempt_anomaly_types)
        self.assertNotIn("alt_in_hostile_entity", override.exempt_anomaly_types)
        expected_expiry = timezone.now() + timedelta(days=scoring().override_days)
        self.assertLess(
            abs((override.expires_at - expected_expiry).total_seconds()), 60
        )

    def test_expired_override_no_longer_suppresses(self):
        """An expired override stops exempting findings and is ignored by readers."""
        expired = CIOverride.objects.create(
            user=self.user1,
            reason="Old scout clearance",
            exempt_anomaly_types=["comms_without_pap"],
            expires_at=timezone.now() - timedelta(days=1),
        )
        permanent = CIOverride.objects.create(
            user=self.user1,
            reason="Sanctioned",
            exempt_anomaly_types=["alt_in_hostile_entity"],
        )
        active = set(CIOverride.objects.active().filter(user=self.user1))
        self.assertEqual(active, {permanent})
        self.assertTrue(expired.is_expired)
        self.assertFalse(permanent.is_expired)

    def test_export_investigation_dossier_endpoint(self):
        """Verify export endpoint returns JSON download with valid signature."""
        inv = CIInvestigation.objects.create(subject_user=self.user1, status="pending")
        self.client.force_login(self.reviewer)
        url = reverse("ci:export_investigation_dossier", args=[inv.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("dossier_hash", data)
        self.assertEqual(data["subject_user_id"], self.user1.id)

    # -------------------------------------------------------------
    # 5. Performance, Caching & Data Retention Tests
    # -------------------------------------------------------------
    def test_prune_ci_telemetry_task(self):
        """Verify automated pruning task deletes old raw events without touching sessions."""
        old_time = timezone.now() - timedelta(days=120)
        recent_time = timezone.now() - timedelta(days=10)

        CIEvent.objects.create(
            source="mumble",
            source_event_id="old_ev",
            timestamp=old_time,
            dedup_hash="hash_old",
        )
        CIEvent.objects.create(
            source="mumble",
            source_event_id="recent_ev",
            timestamp=recent_time,
            dedup_hash="hash_recent",
        )

        prune_ci_telemetry_task(retention_days=90)
        self.assertFalse(CIEvent.objects.filter(dedup_hash="hash_old").exists())
        self.assertTrue(CIEvent.objects.filter(dedup_hash="hash_recent").exists())

    # -------------------------------------------------------------
    # 6. Visual Intelligence & Investigator UX Tests
    # -------------------------------------------------------------
    def test_risk_matrix_view(self):
        """Verify member risk matrix dashboard renders."""
        self.client.force_login(self.reviewer)
        url = reverse("ci:risk_matrix")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alliance Member Risk Matrix")
        self.assertContains(response, "pilot_alpha")

    def test_member_dossier_view(self):
        """Verify member dossier endpoint returns JSON data."""
        self.client.force_login(self.reviewer)
        url = reverse("ci:member_dossier", args=[self.user1.id])
        response = self.client.get(url, {"format": "json"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["subject_username"], "pilot_alpha")
        self.assertIn("integrity_hash", data)
