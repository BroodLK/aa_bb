# Standard Library
from unittest.mock import patch

# Third Party
from celery.exceptions import NotRegistered as CeleryTaskNotRegistered

# Django
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

# AA BigBrother
from aa_bb.models import BigBrotherConfig, ComplianceTicket, ComplianceTicketComment


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
        self.ticket_owner = User.objects.create_user(
            username="ticket_user", password="password"
        )

        cfg = BigBrotherConfig.get_solo()
        cfg.is_active = True
        cfg.save(update_fields=["is_active"])

        self.ticket = ComplianceTicket.objects.create(
            user=self.ticket_owner,
            discord_user_id=123456,
            discord_channel_id=654321,
            reason="corp_check",
            ticket_id=1,
        )

    @patch("aa_bb.tasks_tickets.run_task_function", new=UnregisteredTaskProxy())
    def test_ticket_add_comment_handles_unregistered_discord_task(self):
        self.client.force_login(self.commenter)

        response = self.client.post(
            reverse("aa_bb:ticket_add_comment", args=[self.ticket.pk]),
            {"comment": "Need more time to fix this."},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ComplianceTicketComment.objects.filter(ticket=self.ticket).count(), 1
        )
        comment = ComplianceTicketComment.objects.get(ticket=self.ticket)
        self.assertEqual(comment.user, self.commenter)
        self.assertEqual(comment.comment, "Need more time to fix this.")
