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
    CIOperation,
    CIOperationParticipant,
    CISession,
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

    def _sync_fatlinks(self, fatlinks):
        # Standard Library
        import sys
        import types
        from unittest.mock import MagicMock, patch

        afat_mod = types.ModuleType("afat")
        afat_models_mod = types.ModuleType("afat.models")
        mock_fatlink_cls = MagicMock()
        mock_fatlink_cls.created = MagicMock()
        mock_fatlink_cls.objects.filter.return_value = fatlinks
        afat_models_mod.FatLink = mock_fatlink_cls

        with patch.dict(
            sys.modules, {"afat": afat_mod, "afat.models": afat_models_mod}
        ):
            with patch.object(FCRosterIngestor, "ingest_afat_fleet"):
                return FCRosterIngestor.sync_recent_afat_fleets(hours=24)

    @staticmethod
    def _esi_fatlink(fatlink_id, created, registered):
        # Standard Library
        from types import SimpleNamespace

        return SimpleNamespace(
            id=fatlink_id,
            fleet=f"ESI Fleet {fatlink_id}",
            created=created,
            character=None,
            creator=None,
            is_esilink=True,
            is_registered_on_esi=registered,
        )

    def test_sync_disbanded_esi_fleet_concludes_operation(self):
        fatlink = self._esi_fatlink(901, self.now - timedelta(minutes=40), False)

        op = self._sync_fatlinks([fatlink])[0]
        self.assertIsNotNone(op.end_time)
        self.assertLess(abs((timezone.now() - op.end_time).total_seconds()), 60)
        self.assertTrue(op.is_concluded)
        self.assertEqual(op.status_label, "Concluded")

        # Re-syncing the same disbanded fleet keeps the original close time
        first_end = op.end_time
        op.combat_scanned_at = self.now
        op.save(update_fields=["combat_scanned_at"])
        self._sync_fatlinks([fatlink])
        op.refresh_from_db()
        self.assertEqual(op.end_time, first_end)
        self.assertIsNotNone(op.combat_scanned_at)

    def test_sync_open_esi_fleet_stays_open(self):
        fatlink = self._esi_fatlink(902, self.now - timedelta(minutes=40), True)

        op = self._sync_fatlinks([fatlink])[0]
        self.assertIsNone(op.end_time)
        self.assertTrue(op.is_active)

        # Disbanding the fleet closes the existing operation
        fatlink.is_registered_on_esi = False
        self._sync_fatlinks([fatlink])
        op.refresh_from_db()
        self.assertIsNotNone(op.end_time)
        self.assertTrue(op.is_concluded)

    def test_mark_closed_keeps_existing_end_time(self):
        end = self.now - timedelta(minutes=10)
        op, _ = CIOperationService.find_or_create_operation(
            name="Closed Fleet",
            start_time=self.now - timedelta(hours=1),
            end_time=end,
            external_tag="AFAT FatLink #903",
        )
        op2, created = CIOperationService.find_or_create_operation(
            name="Closed Fleet",
            start_time=self.now - timedelta(hours=1),
            external_tag="AFAT FatLink #903",
            mark_closed=True,
        )
        self.assertFalse(created)
        self.assertEqual(op2.id, op.id)
        op.refresh_from_db()
        self.assertEqual(op.end_time, end)

    def test_roster_deduplication_and_deterministic_ids(self):
        op = CIOperationService.create_operation(
            name="Op Dedup Fleet",
            start_time=self.now - timedelta(hours=1),
            fleet_commander=self.fc_user,
        )

        pasted_text = (
            "[ 2026.10.24 18:00:00 ] pilot_dps > Fleet up\n"
            "[ 2026.10.24 18:01:00 ] pilot_dps > Ready\n"
            "[ 2026.10.24 18:02:00 ] pilot_logi > In system\n"
            "pilot_dps\n"
            "pilot_logi\n"
            "Unresolvable_Pilot_XYZ\n"
            "Unresolvable_Pilot_XYZ\n"
        )

        participants = FCRosterIngestor.ingest_roster_text(
            operation=op,
            text_data=pasted_text,
            fc_user=self.fc_user,
        )

        # pilot_dps (1001), pilot_logi (1002), and Unresolvable_Pilot_XYZ should be deduplicated
        self.assertEqual(len(participants), 2)  # Resolved users pilot1 and pilot2
        self.assertEqual(op.participants.count(), 2)

        # Ingest again with duplicate unresolvable pilot
        parts_extra = FCRosterIngestor.ingest_fc_roster(
            operation=op,
            roster_entries=["Unresolvable_Pilot_XYZ", "Unresolvable_Pilot_XYZ"],
            fc_user=self.fc_user,
        )
        self.assertEqual(
            len(parts_extra), 0
        )  # Unresolved pilot creates CIEvent without CIOperationParticipant

    def test_character_name_resolution_and_timeline_deduplication(self):
        try:
            # Alliance Auth
            from allianceauth.eveonline.models import EveCharacter

            EveCharacter.objects.create(
                character_id=1001,
                character_name="DPS Main",
                corporation_id=1,
                corporation_name="Corp A",
            )
            EveCharacter.objects.create(
                character_id=9001,
                character_name="DPS Cyno Alt",
                corporation_id=1,
                corporation_name="Corp A",
            )
        except Exception:
            pass

        op = CIOperationService.create_operation(
            name="Op Multi-Char Fleet",
            start_time=self.now - timedelta(hours=1),
            fleet_commander=self.fc_user,
        )

        # Add both main and alt for pilot1
        CIOperationParticipant.objects.create(
            operation=op,
            user=self.pilot1,
            character_id=1001,
            confirmed_by_fc=True,
        )
        CIOperationParticipant.objects.create(
            operation=op,
            user=self.pilot1,
            character_id=9001,
            confirmed_by_fc=True,
        )

        timeline = CIOperationService.reconstruct_timeline(op)
        parts = timeline["participants"]

        self.assertEqual(len(parts), 2)
        char_names = {p["character_name"] for p in parts}
        # Names should reflect distinct character names if EveCharacter exists
        self.assertTrue("DPS Main" in char_names or "pilot_dps" in char_names)

    def test_operation_status_properties(self):
        # Open operation started 1 hour ago -> is_active = True
        recent_op = CIOperation.objects.create(
            name="Recent Active Op",
            start_time=self.now - timedelta(hours=1),
        )
        self.assertTrue(recent_op.is_active)
        self.assertFalse(recent_op.is_concluded)
        self.assertEqual(recent_op.status_label, "In Progress")

        # Open operation started 10 hours ago -> is_active = False, is_concluded = True
        stale_op = CIOperation.objects.create(
            name="Stale Op",
            start_time=self.now - timedelta(hours=10),
        )
        self.assertFalse(stale_op.is_active)
        self.assertTrue(stale_op.is_concluded)
        self.assertEqual(stale_op.status_label, "Concluded (Open Window)")

        # Closed operation
        closed_op = CIOperation.objects.create(
            name="Closed Op",
            start_time=self.now - timedelta(hours=3),
            end_time=self.now - timedelta(hours=1),
        )
        self.assertFalse(closed_op.is_active)
        self.assertTrue(closed_op.is_concluded)
        self.assertEqual(closed_op.status_label, "Concluded")

    def test_edit_operation_view(self):
        self.client.force_login(self.officer)
        op = CIOperation.objects.create(
            name="Original Op Name",
            start_time=self.now - timedelta(hours=1),
            operation_type="strategic",
            secrecy_level="standard",
        )

        edit_url = reverse("ci:edit_operation", kwargs={"operation_id": op.id})

        # Test quick action: close_now
        resp_close = self.client.post(edit_url, {"quick_action": "close_now"})
        self.assertEqual(resp_close.status_code, 302)
        op.refresh_from_db()
        self.assertIsNotNone(op.end_time)

        # Test quick action: reopen
        resp_reopen = self.client.post(edit_url, {"quick_action": "reopen"})
        self.assertEqual(resp_reopen.status_code, 302)
        op.refresh_from_db()
        self.assertIsNone(op.end_time)

        # Test editing metadata
        new_start = (self.now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        new_end = (self.now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
        resp_edit = self.client.post(
            edit_url,
            {
                "name": "Updated Op Name",
                "operation_type": "defense",
                "secrecy_level": "classified",
                "start_time": new_start,
                "end_time": new_end,
                "fleet_commander_id": str(self.fc_user.id),
                "notes": "Updated operational brief.",
            },
        )
        self.assertEqual(resp_edit.status_code, 302)
        op.refresh_from_db()
        self.assertEqual(op.name, "Updated Op Name")
        self.assertEqual(op.operation_type, "defense")
        self.assertEqual(op.secrecy_level, "classified")
        self.assertEqual(op.fleet_commander, self.fc_user)
        self.assertEqual(op.notes, "Updated operational brief.")

    def test_add_and_delete_objective_views(self):
        self.client.force_login(self.officer)
        op = CIOperation.objects.create(
            name="Op Objective Test",
            start_time=self.now,
        )

        add_url = reverse("ci:add_objective", kwargs={"operation_id": op.id})
        resp_add = self.client.post(
            add_url,
            {
                "name": "Anchor POS",
                "objective_type": "strategic_defense",
                "expected_roles": "Logi, Tackle",
            },
        )
        self.assertEqual(resp_add.status_code, 302)
        self.assertEqual(op.objectives.count(), 1)
        obj = op.objectives.first()
        self.assertEqual(obj.name, "Anchor POS")
        self.assertEqual(obj.expected_roles, ["Logi", "Tackle"])

        delete_url = reverse(
            "ci:delete_objective",
            kwargs={"operation_id": op.id, "objective_id": obj.id},
        )
        resp_del = self.client.post(delete_url)
        self.assertEqual(resp_del.status_code, 302)
        self.assertEqual(op.objectives.count(), 0)

    def test_add_and_remove_participant_views(self):
        self.client.force_login(self.officer)
        op = CIOperation.objects.create(
            name="Op Participant Test",
            start_time=self.now,
        )

        add_url = reverse("ci:add_participant", kwargs={"operation_id": op.id})
        resp_add = self.client.post(add_url, {"character_input": "pilot_dps"})
        self.assertEqual(resp_add.status_code, 302)
        self.assertEqual(op.participants.count(), 1)
        part = op.participants.first()
        self.assertEqual(part.user, self.pilot1)

        remove_url = reverse(
            "ci:remove_participant",
            kwargs={"operation_id": op.id, "participant_id": part.id},
        )
        resp_rem = self.client.post(remove_url)
        self.assertEqual(resp_rem.status_code, 302)
        self.assertEqual(op.participants.count(), 0)

    def test_auto_detect_comms_view(self):
        self.client.force_login(self.officer)
        op = CIOperation.objects.create(
            name="Op Comms Detect Test",
            start_time=self.now - timedelta(minutes=45),
            end_time=self.now + timedelta(minutes=15),
        )

        # Create active CISession
        CISession.objects.create(
            user=self.pilot2,
            source="mumble",
            channel_name="Fleet 1",
            start_time=self.now - timedelta(minutes=30),
            end_time=self.now,
            reconnect_count=0,
        )

        detect_url = reverse("ci:auto_detect_comms", kwargs={"operation_id": op.id})

        # Run with matching channel
        resp = self.client.post(detect_url, {"channel_keywords": "Fleet 1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(op.participants.count(), 1)
        self.assertEqual(op.participants.first().user, self.pilot2)

    def test_find_or_create_and_deduplicate_operations(self):
        # 1. Create initial in-progress operation
        op1, created1 = CIOperationService.find_or_create_operation(
            name="FIGL Standing",
            start_time=self.now,
            end_time=None,
            operation_type="strategic",
            fleet_commander=self.fc_user,
            external_tag="AFAT FatLink #101",
        )
        self.assertTrue(created1)

        # 2. Subsequent sync with exact external tag updates end_time rather than duplicating
        op2, created2 = CIOperationService.find_or_create_operation(
            name="FIGL Standing",
            start_time=self.now + timedelta(seconds=12),
            end_time=self.now + timedelta(minutes=21),
            operation_type="strategic",
            fleet_commander=self.fc_user,
            external_tag="AFAT FatLink #101",
        )
        self.assertFalse(created2)
        self.assertEqual(op1.id, op2.id)
        op1.refresh_from_db()
        self.assertIsNotNone(op1.end_time)

        # 3. Simulate split creation without tag but within 2-hour window
        op_dup = CIOperation.objects.create(
            name="FIGL Standing",
            start_time=self.now + timedelta(minutes=5),
            end_time=None,
        )
        CIOperationParticipant.objects.create(
            operation=op_dup,
            character_id=99001,
            user=self.pilot1,
            confirmed_by_fc=True,
            recorded_at=self.now,
        )

        merged_count = CIOperationService.deduplicate_operations()
        self.assertEqual(merged_count, 1)
        self.assertFalse(CIOperation.objects.filter(id=op_dup.id).exists())
        self.assertTrue(op1.participants.filter(character_id=99001).exists())

    def test_delete_operation_view(self):
        self.client.force_login(self.officer)
        op = CIOperation.objects.create(
            name="Op To Delete",
            start_time=self.now,
        )
        delete_url = reverse("ci:delete_operation", kwargs={"operation_id": op.id})
        resp = self.client.post(delete_url)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(CIOperation.objects.filter(id=op.id).exists())

    def test_deduplicate_operations_view(self):
        self.client.force_login(self.officer)
        op1 = CIOperation.objects.create(
            name="Duplicate Patrol",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )
        op2 = CIOperation.objects.create(
            name="Duplicate Patrol",
            start_time=self.now + timedelta(minutes=10),
            end_time=None,
        )
        dedup_url = reverse("ci:deduplicate_operations")
        resp = self.client.post(dedup_url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(CIOperation.objects.filter(id=op1.id).exists())
        self.assertFalse(CIOperation.objects.filter(id=op2.id).exists())
