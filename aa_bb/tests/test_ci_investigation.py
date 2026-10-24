"""
Unit and integration tests for CI Investigation Agent, Review Queue, and Case Management.
"""

# Standard Library
from datetime import timedelta

# Django
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership, State, UserProfile
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.counterintel.ingest.mumble import MumbleLogIngestor
from aa_bb.counterintel.models import (
    CICorrelation,
    CIInvestigation,
    CISession,
)
from aa_bb.counterintel.services.agent import CIAgentService
from aa_bb.counterintel.services.identity import IdentityResolver
from aa_bb.counterintel.services.operations import CIOperationService
from aa_bb.models import BigBrotherConfig, General


class TestCIInvestigation(TestCase):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.now = timezone.now()

        # Users
        self.reviewer = User.objects.create_user(
            username="ci_reviewer", email="rev@example.com"
        )
        self.viewer = User.objects.create_user(
            username="ci_viewer", email="view@example.com"
        )
        self.pilot = User.objects.create_user(
            username="suspect_pilot", email="sus@example.com"
        )

        # States & Auth Characters for middleware
        member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        bb_cfg = BigBrotherConfig.get_solo()
        bb_cfg.bb_member_states.set([member_state])
        for idx, u in enumerate([self.reviewer, self.viewer, self.pilot]):
            char, _ = EveCharacter.objects.get_or_create(
                character_id=9900 + idx,
                defaults={
                    "character_name": f"Char_{u.username}",
                    "corporation_id": 1,
                    "corporation_name": "Corp",
                },
            )
            CharacterOwnership.objects.get_or_create(
                character=char, user=u, defaults={"owner_hash": f"h_{idx}"}
            )
            u.profile.main_character = char
            u.profile.state = member_state
            u.profile.save()
            UserProfile.objects.filter(pk=u.profile.pk).update(state=member_state)

        # Permissions
        content_type = ContentType.objects.get_for_model(General)
        view_perm, _ = Permission.objects.get_or_create(
            codename="can_view_ci",
            content_type=content_type,
            defaults={"name": "View CI"},
        )
        review_perm, _ = Permission.objects.get_or_create(
            codename="can_review_ci",
            content_type=content_type,
            defaults={"name": "Review CI"},
        )

        self.viewer.user_permissions.add(view_perm)
        self.reviewer.user_permissions.add(view_perm, review_perm)

        # Clear permission cache
        for u in (self.reviewer, self.viewer, self.pilot):
            if hasattr(u, "_perm_cache"):
                delattr(u, "_perm_cache")
            if hasattr(u, "_user_perm_cache"):
                delattr(u, "_user_perm_cache")

        IdentityResolver.register_identity(
            user=self.pilot,
            character_id=3001,
            mumble_username="suspect_pilot",
            effective_from=self.now - timedelta(days=65),
            is_verified=True,
        )

    def test_agent_synthesis_and_structured_findings(self):
        op = CIOperationService.create_operation(
            name="Op Iron Veil",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
            secrecy_level="high",
        )
        CIOperationService.add_objective(
            operation=op,
            name="Logistics Support Relay",
            objective_type="logi",
            expected_roles=["logistics"],
        )

        # Create correlation finding
        CICorrelation.objects.create(
            operation=op,
            user=self.pilot,
            anomaly_type="comms_without_pap",
            confidence=0.88,
            data_completeness=0.95,
            algorithm_version="1.0.0",
            source_event_ids=["session:1"],
            explanation="Pilot active on command voice without PAP attendance.",
        )

        case = CIAgentService.create_or_update_investigation(
            subject_user=self.pilot,
            operation=op,
            auto_synthesize=True,
        )

        self.assertEqual(case.status, "pending")
        self.assertIsNotNone(case.agent_summary)
        self.assertIn("Investigation Case Summary", case.agent_summary)
        self.assertIn("comms_without_pap", case.agent_summary)
        self.assertIn("Alternative Explanations", case.agent_summary)
        self.assertIn("Logistics", case.agent_summary)  # Role context evaluated
        self.assertIn("Recommended Investigator Next Steps", case.agent_summary)

    def test_auto_triage_operation(self):
        op = CIOperationService.create_operation(
            name="Op Deep Recon",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )

        # High confidence anomaly -> should triage
        MumbleLogIngestor.ingest_raw_event(
            "suspect_pilot",
            "connect",
            self.now + timedelta(minutes=5),
            channel_name="Classified Comms",
        )
        MumbleLogIngestor.ingest_raw_event(
            "suspect_pilot",
            "disconnect",
            self.now + timedelta(minutes=55),
            channel_name="Classified Comms",
        )

        triaged = CIAgentService.auto_triage_operation(op)

        self.assertEqual(len(triaged), 1)
        self.assertEqual(triaged[0].subject_user, self.pilot)
        self.assertEqual(triaged[0].status, "pending")
        self.assertTrue(len(triaged[0].agent_summary) > 0)

    def test_review_queue_and_disposition_workflow(self):
        op = CIOperationService.create_operation(
            name="Op Triage Review",
            start_time=self.now,
        )
        case = CIInvestigation.objects.create(
            operation=op,
            subject_user=self.pilot,
            status="pending",
            agent_summary="Initial synthesis",
        )

        detail_url = reverse(
            "ci:investigation_detail", kwargs={"investigation_id": case.id}
        )
        queue_url = reverse("ci:review_queue")

        # Viewer cannot access review queue
        self.client.force_login(self.viewer)
        resp_view = self.client.get(queue_url)
        self.assertEqual(resp_view.status_code, 302)

        # Reviewer can access queue and see case
        self.client.force_login(self.reviewer)
        resp_rev_queue = self.client.get(queue_url)
        self.assertEqual(resp_rev_queue.status_code, 200)
        self.assertContains(resp_rev_queue, self.pilot.username)

        # Reviewer submits disposition: change status to "cleared" with notes
        post_data = {
            "status": "cleared",
            "reviewer_notes": "Confirmed pilot was on authorized secondary mumble relay.",
        }
        resp_post = self.client.post(detail_url, data=post_data)
        self.assertEqual(resp_post.status_code, 302)

        case.refresh_from_db()
        self.assertEqual(case.status, "cleared")
        self.assertEqual(case.assigned_reviewer, self.reviewer)
        self.assertIn("secondary mumble relay", case.reviewer_notes)

    def test_one_click_quick_dispositions(self):
        op = CIOperationService.create_operation(
            name="Op Quick Action Test",
            start_time=self.now,
        )
        case = CIInvestigation.objects.create(
            operation=op,
            subject_user=self.pilot,
            status="pending",
        )

        quick_url = reverse(
            "ci:quick_disposition", kwargs={"investigation_id": case.id}
        )

        # Test POST one-click clear for scout
        self.client.force_login(self.reviewer)
        resp = self.client.post(quick_url, {"action": "clear_scout"})
        self.assertEqual(resp.status_code, 302)

        case.refresh_from_db()
        self.assertEqual(case.status, "cleared")
        self.assertEqual(case.assigned_reviewer, self.reviewer)
        self.assertIn("Verified Alt / Scout", case.reviewer_notes)

        # Test AJAX one-click escalate
        resp_ajax = self.client.post(
            quick_url,
            {"action": "escalate_senior"},
            headers={"x-requested-with": "XMLHttpRequest"},
        )
        self.assertEqual(resp_ajax.status_code, 200)
        data = resp_ajax.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], "escalated")

        case.refresh_from_db()
        self.assertEqual(case.status, "escalated")
        self.assertIn("senior counterintelligence leadership", case.reviewer_notes)

    def test_zero_overhead_roster_ingest_and_auto_detect(self):
        op = CIOperationService.create_operation(
            name="Op Zero Overhead Fleet",
            start_time=self.now,
            end_time=self.now + timedelta(hours=1),
        )

        manage_perm, _ = Permission.objects.get_or_create(
            codename="can_manage_ci",
            content_type=ContentType.objects.get_for_model(General),
            defaults={"name": "Manage CI"},
        )
        self.reviewer.user_permissions.add(manage_perm)
        self.client.force_login(self.reviewer)

        # 1. Test Quick Roster Paste endpoint
        paste_url = reverse("ci:quick_roster_ingest", kwargs={"operation_id": op.id})
        raw_comp_text = f"[ 2026.10.22 21:00:00 ] {self.pilot.username} > In fleet\n3001\tScimitar\tWing 1"
        resp_paste = self.client.post(paste_url, {"roster_text": raw_comp_text})
        self.assertEqual(resp_paste.status_code, 302)

        self.assertTrue(op.participants.filter(user=self.pilot).exists())

        # 2. Test Auto-Detect Comms Fleet endpoint
        auto_url = reverse("ci:auto_detect_comms", kwargs={"operation_id": op.id})
        CISession.objects.create(
            source="mumble",
            user=self.viewer,
            start_time=self.now + timedelta(minutes=10),
            end_time=self.now + timedelta(minutes=40),
            channel_name="Fleet Comms 1",
        )
        resp_auto = self.client.post(auto_url)
        self.assertEqual(resp_auto.status_code, 302)

        self.assertTrue(op.participants.filter(user=self.viewer).exists())

    def test_benign_explanations_extraction(self):
        op = CIOperationService.create_operation(
            name="Op Benign Check",
            start_time=self.now,
        )
        CIOperationService.add_objective(
            operation=op,
            name="Infiltration Recon",
            objective_type="recon",
            expected_roles=["scout"],
        )
        case = CIInvestigation.objects.create(
            operation=op,
            subject_user=self.pilot,
            status="pending",
        )

        explanations = CIAgentService.get_benign_explanations(case)
        self.assertTrue(len(explanations) > 0)
        categories = [e["category"] for e in explanations]
        self.assertIn("Operational Role Context", categories)

    def test_graceful_degradation_on_external_api_errors(self):
        # Standard Library
        from unittest.mock import patch

        # AA BigBrother
        from aa_bb.app_settings import resolve_character_name, resolve_corporation_name

        with patch(
            "aa_bb.app_settings.ESIHandler.post_universe_names",
            side_effect=Exception("404 HTTPClientError"),
        ):
            corp_name = resolve_corporation_name(999999)
            self.assertEqual(corp_name, "Corporation 999999")

            char_name = resolve_character_name(888888)
            self.assertEqual(char_name, "Character 888888")

    def test_member_list_designated_states_filtering(self):
        bb_cfg = BigBrotherConfig.get_solo()
        member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        guest_state, _ = State.objects.get_or_create(
            name="Guest", defaults={"priority": 200}
        )
        bb_cfg.bb_member_states.set([member_state])

        # Create a guest user not in designated states
        guest_user = User.objects.create_user(
            username="guest_pilot", email="guest@example.com"
        )
        guest_char, _ = EveCharacter.objects.get_or_create(
            character_id=99988,
            defaults={
                "character_name": "Guest Pilot Char",
                "corporation_id": 2,
                "corporation_name": "Guest Corp",
            },
        )
        CharacterOwnership.objects.get_or_create(
            character=guest_char, user=guest_user, defaults={"owner_hash": "h_guest"}
        )
        guest_user.profile.main_character = guest_char
        guest_user.profile.state = guest_state
        guest_user.profile.save()
        UserProfile.objects.filter(pk=guest_user.profile.pk).update(state=guest_state)

        self.client.force_login(self.viewer)
        url = reverse("ci:member_list")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)

        # Designated members should be present
        members = resp.context["page_obj"].object_list
        self.assertIn(self.pilot, members)
        self.assertIn(self.viewer, members)
        self.assertIn(self.reviewer, members)

        # Guest user should NOT be present
        self.assertNotIn(guest_user, members)

        # Search for guest user should yield no results
        resp_search_guest = self.client.get(url, {"q": "guest_pilot"})
        self.assertEqual(resp_search_guest.status_code, 200)
        self.assertEqual(len(resp_search_guest.context["page_obj"].object_list), 0)

        # Search for valid member should yield that member
        resp_search_member = self.client.get(url, {"q": "suspect_pilot"})
        self.assertEqual(resp_search_member.status_code, 200)
        self.assertIn(self.pilot, resp_search_member.context["page_obj"].object_list)

    def test_member_activity_designated_states_access(self):
        bb_cfg = BigBrotherConfig.get_solo()
        member_state, _ = State.objects.get_or_create(
            name="Member", defaults={"priority": 100}
        )
        guest_state, _ = State.objects.get_or_create(
            name="Guest", defaults={"priority": 200}
        )
        bb_cfg.bb_member_states.set([member_state])

        guest_user = User.objects.create_user(
            username="guest_pilot_2", email="guest2@example.com"
        )
        guest_char, _ = EveCharacter.objects.get_or_create(
            character_id=99989,
            defaults={
                "character_name": "Guest Pilot Char 2",
                "corporation_id": 2,
                "corporation_name": "Guest Corp",
            },
        )
        CharacterOwnership.objects.get_or_create(
            character=guest_char, user=guest_user, defaults={"owner_hash": "h_guest2"}
        )
        guest_user.profile.main_character = guest_char
        guest_user.profile.state = guest_state
        guest_user.profile.save()
        UserProfile.objects.filter(pk=guest_user.profile.pk).update(state=guest_state)

        self.client.force_login(self.viewer)

        # Member activity for designated member -> 200
        resp_member = self.client.get(
            reverse("ci:member_activity", kwargs={"user_id": self.pilot.id})
        )
        self.assertEqual(resp_member.status_code, 200)

        # Member activity for non-designated state user -> 404
        resp_guest = self.client.get(
            reverse("ci:member_activity", kwargs={"user_id": guest_user.id})
        )
        self.assertEqual(resp_guest.status_code, 404)

    def test_member_activity_and_list_discord_chat_tracking(self):
        # AA BigBrother
        from aa_bb.models import UserStatus

        # Pilot with recent Discord message
        msg_time = self.now - timedelta(hours=2)
        UserStatus.objects.create(user=self.pilot, last_discord_message_at=msg_time)

        self.client.force_login(self.viewer)

        # 1. Member Directory shows Last Discord Chat
        resp_list = self.client.get(reverse("ci:member_list"))
        self.assertEqual(resp_list.status_code, 200)
        self.assertContains(resp_list, "Last Discord Chat")
        self.assertContains(resp_list, "ago")

        # 2. Member Activity View includes Discord Text Activity context and card
        resp_activity = self.client.get(
            reverse("ci:member_activity", kwargs={"user_id": self.pilot.id})
        )
        self.assertEqual(resp_activity.status_code, 200)
        self.assertEqual(resp_activity.context["last_discord_chat"], msg_time)
        self.assertContains(resp_activity, "Discord Text Activity")
        self.assertContains(resp_activity, "Last Message Sent")

    def test_agent_synthesis_with_discord_chat_context(self):
        # AA BigBrother
        from aa_bb.models import UserStatus

        op = CIOperationService.create_operation(
            name="Op Silent Watch",
            start_time=self.now,
            end_time=self.now + timedelta(hours=2),
        )
        # Pilot sent a Discord chat during the op window
        UserStatus.objects.create(
            user=self.pilot,
            last_discord_message_at=self.now + timedelta(minutes=30),
        )

        case = CIAgentService.create_or_update_investigation(
            subject_user=self.pilot,
            operation=op,
            auto_synthesize=True,
        )

        explanations = CIAgentService.get_benign_explanations(case)
        categories = [e["category"] for e in explanations]
        self.assertIn("Text Communication Presence", categories)

        report = case.agent_summary
        self.assertIn("Discord Text Chat Activity", report)
        self.assertIn("Last message sent", report)
