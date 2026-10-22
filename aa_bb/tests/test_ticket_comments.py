# Standard Library
from unittest.mock import patch

# Third Party
from celery.exceptions import NotRegistered as CeleryTaskNotRegistered

# Django
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase
from django.urls import reverse

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.models import (
    BigBrotherConfig,
    ComplianceTicket,
    ComplianceTicketComment,
    General,
)


class UnregisteredTaskProxy:
    def _get_current_object(self):
        raise CeleryTaskNotRegistered("aadiscordbot.tasks.run_task_function")

    def __getattr__(self, name):
        return getattr(self._get_current_object(), name)

    def apply_async(self, *args, **kwargs):
        raise CeleryTaskNotRegistered("aadiscordbot.tasks.run_task_function")


class TestTicketComments(TestCase):
    def setUp(self):
        self.client = Client()
        self.commenter = User.objects.create_superuser(
            username="ticket_admin",
            password="password",
            email="ticket_admin@example.com",
        )
        # AA wraps plugin URLs in main_character_required, so the commenter
        # needs a main character or every request redirects to the dashboard.
        char = EveCharacter.objects.create(
            character_id=90000001,
            character_name="Ticket Admin",
            corporation_id=1,
            corporation_name="Test Corp",
        )
        CharacterOwnership.objects.create(
            character=char, user=self.commenter, owner_hash="ticket_admin_hash"
        )
        self.commenter.profile.main_character = char
        self.commenter.profile.save()

        self.ticket_owner = User.objects.create_user(
            username="ticket_user", password="password"
        )

        content_type = ContentType.objects.get_for_model(General)
        perm = Permission.objects.filter(
            content_type=content_type, codename="ticket_manager"
        ).first()
        if perm:
            self.commenter.user_permissions.add(perm)

        cfg = BigBrotherConfig.get_solo()
        cfg.is_active = True
        cfg.save()

        self.ticket = ComplianceTicket.objects.create(
            user=self.ticket_owner,
            discord_user_id=123456,
            discord_channel_id=654321,
            reason="corp_check",
            ticket_id=1,
        )

    @patch("aa_bb.tasks_tickets.run_task_function", new=UnregisteredTaskProxy())
    def test_ticket_add_comment_handles_unregistered_discord_task(self):
        self.client.force_login(
            self.commenter, backend="django.contrib.auth.backends.ModelBackend"
        )

        response = self.client.post(
            reverse("aa_bb:ticket_add_comment", args=[self.ticket.pk]),
            {"comment": "Need more time to fix this."},
        )

        self.assertRedirects(
            response,
            reverse("aa_bb:ticket_view", args=[self.ticket.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            ComplianceTicketComment.objects.filter(ticket=self.ticket).count(), 1
        )
        comment = ComplianceTicketComment.objects.get(ticket=self.ticket)
        self.assertEqual(comment.user, self.commenter)
        self.assertEqual(comment.comment, "Need more time to fix this.")
