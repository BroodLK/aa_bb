"""
Unit and integration tests for CI Models, Identity Resolution, and Event Ingestion.
"""

# Standard Library
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

# Django
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

# AA BigBrother
from aa_bb.counterintel.ingest.discord import DiscordIngestor
from aa_bb.counterintel.ingest.mumble import (
    DEFAULT_MUMBLE_ID_OFFSET,
    MumbleLogIngestor,
    MumblePresenceIngestor,
)
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

    def _auth_line(self, ts, message):
        return (
            f"[{ts.strftime('%d/%b/%Y %H:%M:%S')}] INFO "
            f"[mumble_authenticator:599] {message}"
        )

    def test_mumble_authenticator_log_real_lines(self):
        t_fail = (self.now - timedelta(minutes=30)).replace(microsecond=0)
        t_auth = t_fail + timedelta(seconds=20)
        uid = self.user1.pk + DEFAULT_MUMBLE_ID_OFFSET

        log_lines = "\n".join(
            [
                self._auth_line(
                    t_fail,
                    f"Failed authentication attempt for user: Pilot_Alpha {self.user1.pk}",
                ),
                f"[{t_auth.strftime('%d/%b/%Y %H:%M:%S')}] DEBUG "
                "[mumble_authenticator:596] checking password with hash function: "
                "bcrypt-sha256",
                self._auth_line(
                    t_auth, f"User authenticated: [FIGL] Pilot Alpha {uid}"
                ),
                self._auth_line(
                    t_auth, "Group memberships: ['Member', 'Alliance-Flying-Dangerous']"
                ),
                f"[{t_auth.strftime('%d/%b/%Y %H:%M:%S')}] DEBUG "
                "[mumbletagger.signals:24] New / updated mumble tags for Pilot Alpha",
                self._auth_line(t_auth, "Attaching Murmur Meta Callback"),
            ]
        )

        events = MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(
            [ev.payload["action"] for ev in events], ["auth_failed", "connect"]
        )
        for ev in events:
            self.assertEqual(ev.user, self.user1)
        self.assertEqual(events[1].payload["username"], "[FIGL] Pilot Alpha")
        self.assertEqual(events[1].payload["mumble_user_id"], str(uid))
        self.assertEqual(events[1].timestamp, t_auth)

        # Idempotent replay: running again should not duplicate events
        MumbleLogIngestor.ingest_log_text(log_lines)
        self.assertEqual(CIEvent.objects.filter(source="mumble").count(), 2)

        # Authentication opens a session; the log never reports disconnects
        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        self.assertEqual(sessions.first().start_time, t_auth)
        self.assertIsNone(sessions.first().end_time)

    def test_mumble_temp_user_authentication_has_no_auth_user(self):
        ts = (self.now - timedelta(minutes=5)).replace(microsecond=0)
        temp_uid = 7 + 2 * DEFAULT_MUMBLE_ID_OFFSET
        events = MumbleLogIngestor.ingest_log_text(
            self._auth_line(ts, f"TEMP User authenticated: Guest Pilot {temp_uid}")
        )
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0].user)
        self.assertTrue(events[0].payload["temp_user"])
        self.assertEqual(events[0].payload["username"], "Guest Pilot")

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

    def test_mumble_presence_poll_closes_sessions(self):
        t_conn = (self.now - timedelta(minutes=40)).replace(microsecond=0)
        t_disc = self.now - timedelta(minutes=10)
        uid = self.user1.pk + DEFAULT_MUMBLE_ID_OFFSET

        MumbleLogIngestor.ingest_log_text(
            self._auth_line(t_conn, f"User authenticated: Pilot Alpha {uid}")
        )
        mumble_user = SimpleNamespace(
            user=self.user1,
            user_id=self.user1.pk,
            username="Pilot_Alpha",
            # The ICE callback fires a moment after the authenticator logs the login
            last_connect=t_conn + timedelta(milliseconds=700),
            last_disconnect=t_disc,
        )

        created = MumblePresenceIngestor.poll([mumble_user])
        self.assertEqual([ev.payload["action"] for ev in created], ["disconnect"])
        self.assertEqual(
            CIEvent.objects.filter(source="mumble", payload__action="connect").count(),
            1,
        )

        sessions = CISession.objects.filter(user=self.user1, source="mumble")
        self.assertEqual(sessions.count(), 1)
        self.assertEqual(sessions.first().start_time, t_conn)
        self.assertEqual(sessions.first().end_time, t_disc)

        # Polling unchanged timestamps again is a no-op
        self.assertEqual(MumblePresenceIngestor.poll([mumble_user]), [])
        self.assertEqual(CIEvent.objects.filter(source="mumble").count(), 2)

    def test_mumble_out_of_order_disconnect_splits_sessions(self):
        uid = self.user1.pk + DEFAULT_MUMBLE_ID_OFFSET
        t0 = (self.now - timedelta(hours=2)).replace(microsecond=0)
        t1 = t0 + timedelta(minutes=30)
        t2 = t0 + timedelta(minutes=60)

        # Both logins are in the log before the poll observes the disconnect between them
        MumbleLogIngestor.ingest_log_text(
            "\n".join(
                [
                    self._auth_line(t0, f"User authenticated: Pilot Alpha {uid}"),
                    self._auth_line(t2, f"User authenticated: Pilot Alpha {uid}"),
                ]
            )
        )
        MumblePresenceIngestor.poll(
            [
                SimpleNamespace(
                    user=self.user1,
                    user_id=self.user1.pk,
                    username="Pilot_Alpha",
                    last_connect=t2,
                    last_disconnect=t1,
                )
            ]
        )

        sessions = list(
            CISession.objects.filter(user=self.user1, source="mumble").order_by(
                "start_time"
            )
        )
        self.assertEqual(
            [(sess.start_time, sess.end_time) for sess in sessions],
            [(t0, t1), (t2, None)],
        )

    def test_discord_unlinked_author_name_in_timeline(self):
        op = CIOperation.objects.create(
            name="Discord Label Op",
            start_time=self.now - timedelta(minutes=30),
            end_time=self.now + timedelta(minutes=30),
        )
        ev = DiscordIngestor.ingest_message_event(
            discord_user_id=111222333444555666,
            channel_id=42,
            message_id=99,
            timestamp=self.now,
            content_length=12,
            discord_username="spy_account",
            discord_display_name="Totally Friendly",
            channel_name="fleet-chat",
        )
        self.assertIsNone(ev.user)
        self.assertEqual(ev.payload["discord_username"], "spy_account")
        self.assertEqual(ev.payload["channel_name"], "fleet-chat")
        self.assertNotIn("content", ev.payload)

        # AA BigBrother
        from aa_bb.counterintel.services.operations import CIOperationService

        timeline = CIOperationService.reconstruct_timeline(op)
        labels = [e["username"] for e in timeline["events"] if e["id"] == ev.id]
        self.assertEqual(labels, ["Totally Friendly"])

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
