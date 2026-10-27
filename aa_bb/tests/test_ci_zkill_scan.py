"""
Tests for post-operation zKillboard combat scanning.
"""

# Standard Library
from datetime import timedelta
from unittest.mock import MagicMock, patch

# Django
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership, State
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.counterintel.ingest.zkill import ZKillIngestor
from aa_bb.counterintel.models import CIEvent, CIOperation, CIOperationParticipant
from aa_bb.counterintel.services.operations import CIOperationService
from aa_bb.counterintel.tasks import scan_concluded_operations_combat
from aa_bb.models import General


def _zkill_response(entries, content_type="application/json"):
    response = MagicMock()
    response.headers = {"Content-Type": content_type}
    response.json.return_value = entries
    response.status_code = 200
    response.text = ""
    return response


class TestZKillOperationScan(TestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.pilot = User.objects.create_user(username="pilot_scan")
        self.op = CIOperation.objects.create(
            name="Scan Target Op",
            start_time=self.now - timedelta(hours=3),
            end_time=self.now - timedelta(hours=1),
        )
        for character_id in (5001, 5002):
            CIOperationParticipant.objects.create(
                operation=self.op, user=self.pilot, character_id=character_id
            )

        self.killmails = {
            # Inside the window
            900001: {
                "killmail_id": 900001,
                "killmail_time": (self.now - timedelta(hours=2)).isoformat(),
                "solar_system_id": 30000142,
                "victim": {"character_id": 7777},
                "attackers": [{"character_id": 5001}],
            },
            # Long before the operation started
            900002: {
                "killmail_id": 900002,
                "killmail_time": (self.now - timedelta(hours=5)).isoformat(),
                "solar_system_id": 30000142,
                "victim": {"character_id": 5002},
                "attackers": [{"character_id": 8888}],
            },
        }
        self.zkill_entries = {
            5001: [{"killmail_id": 900001, "zkb": {"hash": "h1"}}],
            5002: [
                {"killmail_id": 900002, "zkb": {"hash": "h2"}},
                {"killmail_id": 900001, "zkb": {"hash": "h1"}},
            ],
        }

    def _run_scan(self, session_get):
        session = MagicMock()
        session.get.side_effect = session_get
        with (
            patch("aa_bb.checks.awox._get_requests_session", return_value=session),
            patch("aa_bb.checks.awox._try_acquire_zkill_slot", return_value=True),
            patch.object(
                ZKillIngestor,
                "_fetch_esi_killmail",
                side_effect=lambda kid, _hash: self.killmails[kid],
            ) as esi,
        ):
            events = ZKillIngestor.scan_operation(self.op)
        return events, session, esi

    def _get_from_entries(self, url, timeout=None):
        character_id = int(url.split("/characterID/")[1].split("/")[0])
        return _zkill_response(self.zkill_entries[character_id])

    def test_scan_stores_kills_inside_window_only(self):
        events, session, esi = self._run_scan(self._get_from_entries)

        urls = [c.args[0] for c in session.get.call_args_list]
        self.assertEqual(len(urls), 2)
        self.assertTrue(all("/characterID/500" in url for url in urls))
        # Window starts 3h15m ago, rounded up to whole hours
        self.assertTrue(all(url.endswith("/pastSeconds/14400/") for url in urls))

        # Each unique killmail is hydrated once
        self.assertEqual(esi.call_count, 2)
        self.assertEqual([ev.source_event_id for ev in events], ["zkill:900001"])
        stored = CIEvent.objects.get(source="zkill")
        self.assertEqual(stored.operation, self.op)

        self.op.refresh_from_db()
        self.assertIsNotNone(self.op.combat_scanned_at)

        # A rescan reuses stored killmails instead of hydrating again
        _events, _session, esi_again = self._run_scan(self._get_from_entries)
        self.assertEqual(esi_again.call_count, 1)  # only the out-of-window kill
        self.assertEqual(CIEvent.objects.filter(source="zkill").count(), 1)

    def test_zkill_outage_leaves_operation_unscanned(self):
        def get(url, timeout=None):
            return _zkill_response(None, content_type="text/html")

        with patch("aa_bb.checks.awox._notify_zkill_down_once") as notify:
            self._run_scan(get)
        notify.assert_called()

        self.op.refresh_from_db()
        self.assertIsNone(self.op.combat_scanned_at)

    def test_operations_older_than_zkill_limit_are_marked_scanned(self):
        self.op.start_time = self.now - timedelta(days=9)
        self.op.end_time = self.now - timedelta(days=8)
        self.op.save()

        events, session, _esi = self._run_scan(self._get_from_entries)
        self.assertEqual(events, [])
        session.get.assert_not_called()
        self.op.refresh_from_db()
        self.assertIsNotNone(self.op.combat_scanned_at)

    def test_task_waits_until_window_closed_for_delay(self):
        # Window (end + 15 min) closed only 5 minutes ago
        recent = CIOperation.objects.create(
            name="Just Ended",
            start_time=self.now - timedelta(hours=1),
            end_time=self.now - timedelta(minutes=20),
        )
        already = CIOperation.objects.create(
            name="Already Scanned",
            start_time=self.now - timedelta(hours=4),
            end_time=self.now - timedelta(hours=2),
            combat_scanned_at=self.now - timedelta(hours=1),
        )

        with patch.object(ZKillIngestor, "scan_operation") as scan:
            scanned = scan_concluded_operations_combat(hours=48)

        self.assertEqual(scanned, [self.op.id])
        scanned_ops = [c.args[0].id for c in scan.call_args_list]
        self.assertNotIn(recent.id, scanned_ops)
        self.assertNotIn(already.id, scanned_ops)

    def test_window_change_resets_scan_marker(self):
        self.op.combat_scanned_at = self.now
        self.op.save()

        # AFAT reports a later end time for the same fleet
        op, created = CIOperationService.find_or_create_operation(
            name=self.op.name,
            start_time=self.op.start_time,
            end_time=self.now - timedelta(minutes=10),
        )
        self.assertFalse(created)
        op.refresh_from_db()
        self.assertIsNone(op.combat_scanned_at)

    def test_edit_operation_end_time_resets_scan_marker(self):
        manager = User.objects.create_user(username="ci_manager")
        # Alliance Auth only honours permissions for users with a main and a state
        member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        main = EveCharacter.objects.create(
            character_id=8900,
            character_name="CI Manager Main",
            corporation_id=1,
            corporation_name="Corp",
        )
        CharacterOwnership.objects.create(
            character=main, user=manager, owner_hash="hash_manager"
        )
        manager.profile.main_character = main
        manager.profile.state = member_state
        manager.profile.save()
        perm, _ = Permission.objects.get_or_create(
            codename="can_manage_ci",
            content_type=ContentType.objects.get_for_model(General),
            defaults={"name": "Can manage Counterintelligence operations"},
        )
        manager.user_permissions.add(perm)
        self.client.force_login(manager)

        self.op.combat_scanned_at = self.now
        self.op.save()

        response = self.client.post(
            reverse("ci:edit_operation", args=[self.op.id]),
            {"quick_action": "close_now"},
        )
        self.assertRedirects(
            response,
            reverse("ci:operation_timeline", args=[self.op.id]),
            fetch_redirect_response=False,
        )
        self.op.refresh_from_db()
        self.assertIsNone(self.op.combat_scanned_at)
