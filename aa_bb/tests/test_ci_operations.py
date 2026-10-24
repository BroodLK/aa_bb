"""
Unit and integration tests for CI Operations, FC Roster Ingestion, and Timeline Reconstruction.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

# AA BigBrother
from aa_bb.counterintel.ingest.mumble import MumbleLogIngestor
from aa_bb.counterintel.ingest.paps import FCRosterIngestor
from aa_bb.counterintel.ingest.zkill import ZKillIngestor
from aa_bb.counterintel.models import (
    CIEvent,
    CIInvestigation,
    CIOperationParticipant,
)
from aa_bb.counterintel.services.identity import IdentityResolver
from aa_bb.counterintel.services.operations import CIOperationService


class TestCIOperations(TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.now = timezone.now()

        # Users
        self.fc_user = User.objects.create_user(
            username="fleet_commander_1", email="fc@example.com"
        )
        self.officer = User.objects.create_user(
            username="ci_officer", email="officer@example.com"
        )
        self.reviewer = User.objects.create_user(
            username="ci_reviewer", email="reviewer@example.com"
        )
        self.pilot1 = User.objects.create_user(
            username="pilot_dps", email="dps@example.com"
        )
        self.pilot2 = User.objects.create_user(
            username="pilot_logi", email="logi@example.com"
        )
        self.pilot3 = User.objects.create_user(
            username="pilot_scout", email="scout@example.com"
        )
        self.pilot4 = User.objects.create_user(
            username="pilot_spy", email="spy@example.com"
        )

        # Permissions
        # Django
        from django.contrib.contenttypes.models import ContentType

        # Alliance Auth
        from allianceauth.authentication.models import CharacterOwnership, State
        from allianceauth.eveonline.models import EveCharacter

        # AA BigBrother
        from aa_bb.models import General

        member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        for idx, u in enumerate([self.officer, self.reviewer, self.pilot1]):
            char, _ = EveCharacter.objects.get_or_create(
                character_id=8800 + idx,
                defaults={
                    "character_name": f"Char_{u.username}",
                    "corporation_id": 1,
                    "corporation_name": "Corp",
                },
            )
            CharacterOwnership.objects.get_or_create(
                character=char, user=u, defaults={"owner_hash": f"hash_{idx}"}
            )
            u.profile.main_character = char
            u.profile.state = member_state
            u.profile.save()

        content_type = ContentType.objects.get_for_model(General)
        view_perm, _ = Permission.objects.get_or_create(
            codename="can_view_ci",
            content_type=content_type,
            defaults={"name": "Can view Counterintelligence information"},
        )
        review_perm, _ = Permission.objects.get_or_create(
            codename="can_review_ci",
            content_type=content_type,
            defaults={"name": "Can review Counterintelligence investigations"},
        )
        manage_perm, _ = Permission.objects.get_or_create(
            codename="can_manage_ci",
            content_type=content_type,
            defaults={
                "name": "Can manage Counterintelligence operations and baselines"
            },
        )

        self.officer.user_permissions.add(view_perm, manage_perm)
        self.reviewer.user_permissions.add(view_perm, review_perm)

        # Clear any permission cache
        for u in (self.officer, self.reviewer, self.pilot1):
            if hasattr(u, "_perm_cache"):
                delattr(u, "_perm_cache")
            if hasattr(u, "_user_perm_cache"):
                delattr(u, "_user_perm_cache")

        # Identity mappings
        IdentityResolver.register_identity(
            user=self.pilot1,
            character_id=1001,
            mumble_username="pilot_dps",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot2,
            character_id=1002,
            mumble_username="pilot_logi",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot3,
            character_id=1003,
            mumble_username="pilot_scout",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )
        IdentityResolver.register_identity(
            user=self.pilot4,
            character_id=1004,
            mumble_username="pilot_spy",
            effective_from=self.now - timedelta(days=1),
            is_verified=True,
        )

    def test_operation_creation_and_objectives(self):
        op = CIOperationService.create_operation(
            name="Op Iron Shield",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
            operation_type="strategic",
            fleet_commander=self.fc_user,
            secrecy_level="high",
        )
        self.assertEqual(op.name, "Op Iron Shield")
        self.assertEqual(op.fleet_commander, self.fc_user)

        CIOperationService.add_objective(
            operation=op,
            name="Citadel Defense",
            objective_type="strategic_defense",
            expected_roles=["dps", "logi"],
        )
        CIOperationService.add_objective(
            operation=op,
            name="Cyno Recon",
            objective_type="recon",
            expected_roles=["scout"],
        )
        self.assertEqual(op.objectives.count(), 2)

    def test_fc_roster_ingestion_authoritative_no_member_action(self):
        op = CIOperationService.create_operation(
            name="Op Fortress",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
            fleet_commander=self.fc_user,
        )

        # FC submits list of characters confirmed in fleet
        fc_roster = [
            {"character_id": 1001, "character_name": "Pilot DPS"},
            {"character_id": 1002, "character_name": "Pilot Logi"},
        ]

        participants = FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=fc_roster,
            fc_user=self.fc_user,
            recorded_at=self.now,
        )

        self.assertEqual(len(participants), 2)
        self.assertEqual(CIOperationParticipant.objects.filter(operation=op).count(), 2)
        self.assertTrue(
            CIOperationParticipant.objects.filter(
                operation=op, user=self.pilot1
            ).exists()
        )
        self.assertTrue(
            CIOperationParticipant.objects.filter(
                operation=op, user=self.pilot2
            ).exists()
        )
        self.assertFalse(
            CIOperationParticipant.objects.filter(
                operation=op, user=self.pilot4
            ).exists()
        )

        # CIEvent created for each roster entry
        pap_events = CIEvent.objects.filter(source="pap", operation=op)
        self.assertEqual(pap_events.count(), 2)

    def test_zkill_event_enrichment(self):
        op = CIOperationService.create_operation(
            name="Op Skirmish",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )

        killmail = {
            "killmail_id": 88812345,
            "killmail_time": (self.now + timedelta(minutes=25)).isoformat(),
            "solar_system_id": 30000142,
            "victim": {
                "character_id": 99999,
                "ship_type_id": 12005,
            },
            "attackers": [
                {"character_id": 1001, "damage_done": 5000, "final_blow": True},
            ],
        }

        event = ZKillIngestor.ingest_killmail(killmail, operation=op)
        self.assertIsNotNone(event.pk)
        self.assertEqual(event.user, self.pilot1)
        self.assertEqual(event.operation, op)

    def test_timeline_reconstruction_and_overlap_metrics(self):
        op_start = self.now
        op_end = self.now + timedelta(hours=2)
        op = CIOperationService.create_operation(
            name="Op Anomaly Check",
            start_time=op_start,
            end_time=op_end,
            fleet_commander=self.fc_user,
        )

        # FC credits pilot1 and pilot2
        FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=[1001, 1002],
            fc_user=self.fc_user,
            recorded_at=op_start,
        )

        # Mumble comms: pilot1 (confirmed), pilot2 (confirmed), pilot4 (NOT on roster - spy/observer)
        MumbleLogIngestor.ingest_raw_event(
            "pilot_dps",
            "connect",
            op_start + timedelta(minutes=5),
            channel_name="Fleet 1",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_logi",
            "connect",
            op_start + timedelta(minutes=5),
            channel_name="Fleet 1",
        )
        MumbleLogIngestor.ingest_raw_event(
            "pilot_spy",
            "connect",
            op_start + timedelta(minutes=2),
            channel_name="Fleet 1",
        )

        timeline = CIOperationService.reconstruct_timeline(op)

        self.assertEqual(timeline["metrics"]["total_confirmed_participants"], 2)
        self.assertEqual(timeline["metrics"]["total_sessions"], 3)
        # pilot4 is in comms but not on PAP roster
        self.assertEqual(timeline["metrics"]["comms_without_pap_count"], 1)

    def test_permission_and_view_access(self):
        op = CIOperationService.create_operation(
            name="Op Restricted",
            start_time=self.now,
        )
        CIInvestigation.objects.create(
            operation=op,
            subject_user=self.pilot4,
            status="pending",
        )

        timeline_url = reverse("ci:operation_timeline", kwargs={"operation_id": op.id})
        queue_url = reverse("ci:review_queue")

        # Anonymous / unauthenticated gets redirected to login
        resp = self.client.get(timeline_url)
        self.assertEqual(resp.status_code, 302)

        # Regular pilot without permissions gets 302/403
        self.client.force_login(self.pilot1)
        resp_pilot = self.client.get(timeline_url)
        self.assertEqual(resp_pilot.status_code, 302)  # permission_required redirects

        # CI Officer with can_view_ci gets 200 on timeline and member view
        self.client.force_login(self.officer)
        resp_officer = self.client.get(timeline_url)
        self.assertEqual(resp_officer.status_code, 200)

        # CI Officer without can_review_ci cannot access review queue
        resp_officer_queue = self.client.get(queue_url)
        self.assertEqual(resp_officer_queue.status_code, 302)

        # CI Reviewer with can_review_ci gets 200 on review queue
        self.client.force_login(self.reviewer)
        resp_reviewer = self.client.get(queue_url)
        self.assertEqual(resp_reviewer.status_code, 200)

    def test_sync_recent_afat_fleets_with_completed_updates(self):
        # Standard Library
        import sys
        import types
        from unittest.mock import MagicMock, patch

        # Create mock module for afat if not present
        afat_mod = types.ModuleType("afat")
        afat_models_mod = types.ModuleType("afat.models")

        mock_fatlink = MagicMock()
        mock_fatlink.id = 777
        mock_fatlink.fleet = "Op Strategic Recon"
        mock_fatlink.created = self.now - timedelta(hours=2)
        mock_fatlink.closed_at = self.now - timedelta(hours=1)
        mock_fatlink.character = MagicMock(character_id=8800)

        mock_fatlink_cls = MagicMock()
        mock_fatlink_cls.created = MagicMock()
        mock_fatlink_cls.objects.filter.return_value = [mock_fatlink]
        afat_models_mod.FatLink = mock_fatlink_cls

        with patch.dict(
            sys.modules, {"afat": afat_mod, "afat.models": afat_models_mod}
        ):
            with patch.object(FCRosterIngestor, "ingest_afat_fleet") as mock_ingest:
                synced = FCRosterIngestor.sync_recent_afat_fleets(hours=24)
                self.assertEqual(len(synced), 1)
                op = synced[0]
                self.assertEqual(op.name, "Op Strategic Recon")
                self.assertEqual(op.fleet_commander, self.officer)
                self.assertIsNotNone(op.end_time)
                mock_ingest.assert_called_once_with(op, 777)
