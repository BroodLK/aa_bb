from django.contrib.auth.models import Group, User
from django.test import TestCase

from allianceauth.groupmanagement.models import RequestLog

from aa_bb.models import AdminLogEntry


class RequestLogSignalTests(TestCase):
    """RequestLog post_save handler must work with the AA 5.6 schema."""

    def setUp(self):
        self.group = Group.objects.create(name="Test Group")
        self.actor = User.objects.create_user("manager")
        self.requestor = User.objects.create_user("member")

    def _create_log(self, request_type, action, requestor=None):
        kwargs = {
            "group": self.group,
            "request_actor": self.actor,
            "request_type": request_type,
            "action": action,
        }
        if hasattr(RequestLog, "requestor"):
            kwargs["requestor"] = requestor
        else:
            kwargs["request_info"] = (
                f"{requestor.username}:Char" if requestor else ""
            )
        return RequestLog.objects.create(**kwargs)

    def _latest_entry(self):
        return AdminLogEntry.objects.filter(source="groupmanagement").latest("pk")

    def test_join_accepted(self):
        if hasattr(RequestLog, "Type"):
            log = self._create_log("join", "accept", self.requestor)
        else:
            log = self._create_log(False, True, self.requestor)
        entry = self._latest_entry()
        self.assertEqual(entry.action, "group_request_join_accepted")
        self.assertEqual(entry.actor, self.actor)
        self.assertEqual(entry.target_user, self.requestor)
        self.assertEqual(entry.target_label, self.group.name)
        self.assertEqual(entry.metadata["request_log_id"], log.pk)
        self.assertEqual(entry.metadata["request_info"], "member" if hasattr(RequestLog, "Type") else "member:Char")

    def test_leave_rejected(self):
        if hasattr(RequestLog, "Type"):
            self._create_log("leave", "reject", self.requestor)
        else:
            self._create_log(True, False, self.requestor)
        self.assertEqual(self._latest_entry().action, "group_request_leave_rejected")

    def test_removed_without_requestor(self):
        if hasattr(RequestLog, "Type"):
            self._create_log("remove", "remove", None)
        else:
            self._create_log(None, True, None)
        entry = self._latest_entry()
        self.assertEqual(entry.action, "group_request_removed_accepted")
        self.assertIsNone(entry.target_user)
