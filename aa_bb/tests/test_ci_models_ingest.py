"""
Unit and integration tests for CI Models, Identity Resolution, and Event Ingestion.
"""

# Standard Library
from datetime import timedelta
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

# AA BigBrother
from aa_bb.counterintel.ingest.discord import DiscordIngestor
from aa_bb.counterintel.ingest.mumble import MumbleLogIngestor
from aa_bb.counterintel.models import (
    CICorrelation,
    CIEvent,
    CIInvestigation,
    CIOperation,
    CIOperationObjective,
    CIOperationParticipant,
    CIOverride,
    CISession,
)
from aa_bb.counterintel.services.identity import IdentityResolver


class TestCIModelsAndIngest(TestCase):
    def setUp(self):
        super().setUp()
        self.user1 = User.objects.create_user(
            username="pilot_alpha", email="alpha@example.com"
        )
        self.user2 = User.objects.create_user(
            username="pilot_bravo", email="bravo@example.com"
        )
        self.now = timezone.now()

    def test_ci_identity_effective_dates(self):
        t0 = self.now - timedelta(days=10)
        t1 = self.now - timedelta(days=5)
        t2 = self.now

        # Initial identity: pilot_alpha had mumble username "AlphaOld"
        IdentityResolver.register_identity(
            user=self.user1,
            character_id=90001,
            mumble_username="AlphaOld",
            effective_from=t0,
            effective_until=t1,
            is_verified=True,
        )

        # Later identity: pilot_alpha changed mumble username to "AlphaNew"
        IdentityResolver.register_identity(
            user=self.user1,
            character_id=90001,
            mumble_username="AlphaNew",
            effective_from=t1,
            is_verified=True,
        )

        # Resolution at past time t0 + 1 day should resolve to user1 via AlphaOld
        resolved_past = IdentityResolver.resolve_user_by_mumble(
            "AlphaOld", at_time=t0 + timedelta(days=1)
        )
        self.assertEqual(resolved_past, self.user1)

        # Resolution at past time via AlphaNew should be None
        resolved_past_new = IdentityResolver.resolve_user_by_mumble(
            "AlphaNew", at_time=t0 + timedelta(days=1)
        )
        self.assertIsNone(resolved_past_new)

        # Resolution at current time via AlphaNew should resolve to user1
        resolved_current = IdentityResolver.resolve_user_by_mumble(
            "AlphaNew", at_time=t2
        )
        self.assertEqual(resolved_current, self.user1)

        # Resolution at current time via AlphaOld should be None
        resolved_expired = IdentityResolver.resolve_user_by_mumble(
            "AlphaOld", at_time=t2
        )
        self.assertIsNone(resolved_expired)

    def test_ci_operation_and_participant_constraints(self):
        op = CIOperation.objects.create(
            name="Op Titanfall",
            operation_type="strategic",
            start_time=self.now,
            fleet_commander=self.user1,
        )
        self.assertIn("Op Titanfall", str(op))

        obj = CIOperationObjective.objects.create(
            operation=op,
            name="Defense Objective",
            objective_type="strategic_defense",
            expected_roles=["logi", "dps"],
        )
        self.assertEqual(obj.operation, op)

        part = CIOperationParticipant.objects.create(
            operation=op,
            user=self.user2,
            character_id=90002,
            confirmed_by_fc=True,
        )
        self.assertEqual(part.user, self.user2)

        # Unique constraint on (operation, character_id)
        with self.assertRaises(IntegrityError):
            CIOperationParticipant.objects.create(
                operation=op,
                user=self.user1,
                character_id=90002,
                confirmed_by_fc=True,
            )

    def test_ci_event_deduplication(self):
        source_id = "test_event_1"
        payload = {"foo": "bar"}
        hash1 = CIEvent.generate_dedup_hash("mumble", source_id, self.now, payload)
        hash2 = CIEvent.generate_dedup_hash("mumble", source_id, self.now, payload)
        self.assertEqual(hash1, hash2)

        ev1 = CIEvent.objects.create(
            source="mumble",
            source_event_id=source_id,
            timestamp=self.now,
            user=self.user1,
            payload=payload,
            dedup_hash=hash1,
        )
        self.assertIsNotNone(ev1.pk)

        # Duplicate insert fails on dedup_hash
        with self.assertRaises(IntegrityError):
            CIEvent.objects.create(
                source="mumble",
                source_event_id=source_id,
                timestamp=self.now,
                user=self.user1,
                payload=payload,
                dedup_hash=hash1,
            )

    def test_mumble_log_ingestor_parsing_and_sessions(self):
        IdentityResolver.register_identity(
            user=self.user1,
            mumble_username="pilot_alpha",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

        t_conn = self.now - timedelta(minutes=30)
        t_move = self.now - timedelta(minutes=20)
        t_disc = self.now - timedelta(minutes=5)

        log_lines = f"""
        [{t_conn.isoformat()}] pilot_alpha AUTHENTICATE session=101 channel="Lobby"
        [{t_move.isoformat()}] pilot_alpha CHANNEL_MOVE session=101 channel="Fleet Alpha"
        [{t_disc.isoformat()}] pilot_alpha DISCONNECT session=101 channel="Fleet Alpha"
        """

        events = MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(len(events), 3)

        # Idempotent replay: running again should not duplicate events
        events_replay = MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(len(events_replay), 3)
        self.assertEqual(CIEvent.objects.filter(source="mumble").count(), 3)

        # Session verification
        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        sess = sessions.first()
        self.assertEqual(sess.channel_name, "Fleet Alpha")
        self.assertIsNotNone(sess.end_time)
        self.assertGreater(sess.duration_seconds, 0)

    def test_mumble_reconnect_clustering(self):
        IdentityResolver.register_identity(
            user=self.user1,
            mumble_username="pilot_alpha",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

        t0 = self.now - timedelta(minutes=30)
        t1 = self.now - timedelta(minutes=15)
        t2 = self.now - timedelta(minutes=14)  # 1 min drop -> reconnect

        MumbleLogIngestor.ingest_raw_event(
            username="pilot_alpha",
            action="connect",
            timestamp=t0,
            channel_name="Fleet Comms",
        )
        MumbleLogIngestor.ingest_raw_event(
            username="pilot_alpha",
            action="disconnect",
            timestamp=t1,
            channel_name="Fleet Comms",
        )
        MumbleLogIngestor.ingest_raw_event(
            username="pilot_alpha",
            action="connect",
            timestamp=t2,
            channel_name="Fleet Comms",
        )

        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        sess = sessions.first()
        self.assertEqual(sess.reconnect_count, 1)
        self.assertIsNone(sess.end_time)

    def test_discord_ingestor_voice_and_messages(self):
        discord_uid = 123456789012345678
        IdentityResolver.register_identity(
            user=self.user2,
            discord_id=discord_uid,
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

        t_join = self.now - timedelta(minutes=20)
        t_msg = self.now - timedelta(minutes=15)
        t_leave = self.now - timedelta(minutes=5)

        ev_join = DiscordIngestor.ingest_voice_state_update(
            discord_user_id=discord_uid,
            channel_id=987654321,
            channel_name="Command Voice",
            action="join",
            timestamp=t_join,
        )
        self.assertEqual(ev_join.user, self.user2)

        ev_msg = DiscordIngestor.ingest_message_event(
            discord_user_id=discord_uid,
            channel_id=987654321,
            message_id=55555,
            timestamp=t_msg,
            content_length=42,
        )
        self.assertEqual(ev_msg.user, self.user2)
        self.assertNotIn("content", ev_msg.payload)  # Privacy requirement

        # AA BigBrother
        from aa_bb.models import UserStatus

        status = UserStatus.objects.get(user=self.user2)
        self.assertEqual(status.last_discord_message_at, t_msg)

        ev_leave = DiscordIngestor.ingest_voice_state_update(
            discord_user_id=discord_uid,
            channel_id=987654321,
            channel_name="Command Voice",
            action="leave",
            timestamp=t_leave,
        )
        self.assertEqual(ev_leave.user, self.user2)

        sessions = CISession.objects.filter(user=self.user2, source="discord")
        self.assertEqual(sessions.count(), 1)
        sess = sessions.first()
        self.assertEqual(sess.channel_name, "Command Voice")
        self.assertIsNotNone(sess.end_time)

    def test_ci_correlation_investigation_and_override_models(self):
        op = CIOperation.objects.create(
            name="Op Blackout",
            start_time=self.now,
        )
        corr = CICorrelation.objects.create(
            operation=op,
            user=self.user1,
            anomaly_type="comms_without_pap",
            confidence=0.85,
            data_completeness=0.95,
            algorithm_version="1.0.0",
            source_event_ids=["mumble:1", "mumble:2"],
            explanation="Pilot Alpha was active in comms but not on roster.",
        )
        self.assertIn("0.85", str(corr))

        inv = CIInvestigation.objects.create(
            operation=op,
            subject_user=self.user1,
            status="pending",
            agent_summary="Initial summary",
        )
        self.assertEqual(inv.status, "pending")
        self.assertIn("Needs Review", str(inv))

        ovr = CIOverride.objects.create(
            user=self.user1,
            reason="Approved stealth scout on alternate comms",
            exempt_anomaly_types=["comms_without_pap"],
            created_by=self.user2,
        )
        self.assertIn("comms_without_pap", str(ovr))

    def test_mumble_authenticator_log_formats(self):
        IdentityResolver.register_identity(
            user=self.user1,
            mumble_username="pilot_alpha",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

        log_lines = """
        2026-10-23 18:22:01,123 - authenticator - INFO - Authenticating user: pilot_alpha
        2026-10-23 18:22:02,456 - authenticator - INFO - userConnected: 42 (pilot_alpha)
        2026-10-23 18:45:00,789 - authenticator - INFO - User pilot_alpha moved to channel Fleet 1
        2026-10-23 19:10:00,123 - authenticator - INFO - userDisconnected: 42 (pilot_alpha)
        """
        events = MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(len(events), 4)

        # Check all resolved to user1
        for ev in events:
            self.assertEqual(ev.user, self.user1)

        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        sess = sessions.first()
        self.assertEqual(sess.channel_name, "Fleet 1")
        self.assertIsNotNone(sess.end_time)
        self.assertGreater(sess.duration_seconds, 0)

    def test_mumble_disconnect_by_session_id_resolution(self):
        IdentityResolver.register_identity(
            user=self.user1,
            mumble_username="pilot_alpha",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

        # First line connects with session id 88, second disconnects with session id only
        log_lines = """
        2026-10-23 18:22:01,123 - authenticator - INFO - userConnected: 88 (pilot_alpha)
        2026-10-23 18:35:00,456 - authenticator - INFO - userDisconnected: 88
        """
        events = MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].user, self.user1)
        self.assertEqual(events[1].user, self.user1)

        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        self.assertIsNotNone(sessions.first().end_time)

    @patch("aa_bb.tasks_tickets.discordbot_active", return_value=True)
    def test_discord_last_message_resolution_and_fallback(self, mock_bot_active):
        # AA BigBrother
        from aa_bb.models import BigBrotherConfig, TicketToolConfig, UserStatus
        from aa_bb.tasks_tickets import discord_inactivity_check

        discord_uid = 998877665544332211
        IdentityResolver.register_identity(
            user=self.user1,
            discord_id=discord_uid,
            effective_from=self.now - timedelta(days=10),
            is_verified=True,
        )

        # 1. Initially no message recorded
        self.assertIsNone(UserStatus.get_last_discord_message_at_for_user(self.user1))

        # 2. Ingest message through CIEvent
        msg_time_1 = self.now - timedelta(days=2)
        DiscordIngestor.ingest_message_event(
            discord_user_id=discord_uid,
            channel_id=111,
            message_id=222,
            timestamp=msg_time_1,
            content_length=10,
        )
        resolved_ts = UserStatus.get_last_discord_message_at_for_user(self.user1)
        self.assertEqual(resolved_ts, msg_time_1)

        # 3. Out-of-order older message should not overwrite newer timestamp
        older_msg_time = self.now - timedelta(days=5)
        DiscordIngestor.ingest_message_event(
            discord_user_id=discord_uid,
            channel_id=111,
            message_id=221,
            timestamp=older_msg_time,
            content_length=10,
        )
        resolved_ts = UserStatus.get_last_discord_message_at_for_user(self.user1)
        self.assertEqual(resolved_ts, msg_time_1)

        # 4. Fallback recovery: Delete UserStatus record, verify fallback to CIEvent restores it
        UserStatus.objects.filter(user=self.user1).delete()
        fallback_ts = UserStatus.get_last_discord_message_at_for_user(self.user1)
        self.assertEqual(fallback_ts, msg_time_1)
        self.assertTrue(UserStatus.objects.filter(user=self.user1).exists())

        # 5. Inactivity check evaluation
        tcfg = TicketToolConfig.get_solo()
        bbcfg = BigBrotherConfig.get_solo()
        tcfg.discord_inactivity_enabled = True
        tcfg.discord_inactivity_days = 7
        tcfg.save()
        bbcfg.discord_message_tracking = True
        bbcfg.save()

        # Recent message (2 days ago < 7 days limit) -> returns True (Active)
        self.assertTrue(discord_inactivity_check(self.user1))

        # Stale message (10 days ago >= 7 days limit) -> returns False (Inactive)
        UserStatus.objects.filter(user=self.user1).update(
            last_discord_message_at=self.now - timedelta(days=10)
        )
        self.assertFalse(discord_inactivity_check(self.user1))
