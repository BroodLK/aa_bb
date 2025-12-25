from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth.models import User, Permission
from django.contrib.contenttypes.models import ContentType
from allianceauth.authentication.models import UserProfile, State, CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from aa_bb.models import BigBrotherConfig, General
from aa_bb.views import get_available_cards, CARD_DEFINITIONS
import json
from unittest.mock import patch

class TestDashboardViews(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Create states with unique names and likely unique priorities
        cls.member_state, _ = State.objects.get_or_create(name="BB_Member", defaults={'priority': 10001})
        cls.guest_state, _ = State.objects.get_or_create(name="BB_Guest", defaults={'priority': 10002})

        # Create users with passwords
        cls.admin_user = User.objects.create_user(username="admin", password="password")
        cls.recruiter_user = User.objects.create_user(username="recruiter", password="password")
        cls.basic_user = User.objects.create_user(username="basic", password="password")
        cls.super_user = User.objects.create_superuser(username="superuser", password="password", email="admin@example.com")

        # Add permissions
        content_type = ContentType.objects.get_for_model(General)
        perms = Permission.objects.filter(content_type=content_type)

        basic_access = perms.get(codename="basic_access")
        full_access = perms.get(codename="full_access")
        recruiter_access = perms.get(codename="recruiter_access")

        cls.admin_user.user_permissions.add(basic_access, full_access)
        cls.recruiter_user.user_permissions.add(basic_access, recruiter_access)
        cls.basic_user.user_permissions.add(basic_access)

        # Refresh users to ensure permissions are loaded
        cls.admin_user = User.objects.get(pk=cls.admin_user.pk)
        cls.recruiter_user = User.objects.get(pk=cls.recruiter_user.pk)
        cls.basic_user = User.objects.get(pk=cls.basic_user.pk)

        # Create config
        cls.config = BigBrotherConfig.get_solo()
        cls.config.is_active = True
        cls.config.bb_member_states.add(cls.member_state)
        cls.config.bb_guest_states.add(cls.guest_state)
        cls.config.alliance_blacklist_url = "http://alliance.bl"
        cls.config.external_blacklist_url = "http://external.bl"
        cls.config.save()

        # Create characters and ownership
        # We give the "viewer" users the Member state so they don't show up in Guest lists
        cls.char_admin = EveCharacter.objects.create(character_id=100, character_name="Admin Char", corporation_id=1, corporation_name="Admin Corp")
        CharacterOwnership.objects.create(character=cls.char_admin, user=cls.admin_user, owner_hash="h0")
        cls.admin_user.profile.main_character = cls.char_admin
        cls.admin_user.profile.state = cls.member_state
        cls.admin_user.profile.save()
        UserProfile.objects.filter(pk=cls.admin_user.profile.pk).update(state=cls.member_state)

        cls.char_recruiter = EveCharacter.objects.create(character_id=101, character_name="Recruiter Char", corporation_id=1, corporation_name="Member Corp")
        CharacterOwnership.objects.create(character=cls.char_recruiter, user=cls.recruiter_user, owner_hash="h11")
        cls.recruiter_user.profile.main_character = cls.char_recruiter
        cls.recruiter_user.profile.state = cls.member_state
        cls.recruiter_user.profile.save()
        UserProfile.objects.filter(pk=cls.recruiter_user.profile.pk).update(state=cls.member_state)

        cls.char_basic = EveCharacter.objects.create(character_id=102, character_name="Basic Char", corporation_id=1, corporation_name="Member Corp")
        CharacterOwnership.objects.create(character=cls.char_basic, user=cls.basic_user, owner_hash="h12")
        cls.basic_user.profile.main_character = cls.char_basic
        cls.basic_user.profile.state = cls.member_state
        cls.basic_user.profile.save()
        UserProfile.objects.filter(pk=cls.basic_user.profile.pk).update(state=cls.member_state)

        cls.char_member = EveCharacter.objects.create(character_id=103, character_name="Member Char", corporation_id=1, corporation_name="Member Corp")
        cls.user_member = User.objects.create_user(username="member_user", password="password")
        CharacterOwnership.objects.create(character=cls.char_member, user=cls.user_member, owner_hash="h1")
        cls.user_member.profile.state = cls.member_state
        cls.user_member.profile.main_character = cls.char_member
        cls.user_member.profile.save()
        UserProfile.objects.filter(pk=cls.user_member.profile.pk).update(state=cls.member_state)

        cls.char_guest = EveCharacter.objects.create(character_id=104, character_name="Guest Char", corporation_id=2, corporation_name="Guest Corp")
        cls.user_guest = User.objects.create_user(username="guest_user", password="password")
        CharacterOwnership.objects.create(character=cls.char_guest, user=cls.user_guest, owner_hash="h2")
        cls.user_guest.profile.state = cls.guest_state
        cls.user_guest.profile.main_character = cls.char_guest
        cls.user_guest.profile.save()
        UserProfile.objects.filter(pk=cls.user_guest.profile.pk).update(state=cls.guest_state)

        # Superuser also needs a profile/main character to avoid redirects
        cls.char_super = EveCharacter.objects.create(character_id=105, character_name="Super Char", corporation_id=1, corporation_name="Admin Corp")
        CharacterOwnership.objects.create(character=cls.char_super, user=cls.super_user, owner_hash="h99")
        cls.super_user.profile.main_character = cls.char_super
        cls.super_user.profile.state = cls.member_state
        cls.super_user.profile.save()
        UserProfile.objects.filter(pk=cls.super_user.profile.pk).update(state=cls.member_state)

    def setUp(self):
        self.client = Client()

    def test_index_access(self):
        # Anonymous users should be redirected to login
        response = self.client.get(reverse("aa_bb:index"))
        self.assertEqual(response.status_code, 302)

        # Authorized users should get 200
        self.assertTrue(self.client.login(username="basic", password="password"))
        response = self.client.get(reverse("aa_bb:index"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "aa_bb/index.html")

    def test_index_dropdown_options_full_access(self):
        self.assertTrue(self.client.login(username="admin", password="password"))
        response = self.client.get(reverse("aa_bb:index"))
        self.assertEqual(response.status_code, 200)
        options = response.context['dropdown_options']
        self.assertIn("Member Char", options)
        self.assertIn("Guest Char", options)

    def test_superuser_access(self):
        self.assertTrue(self.client.login(username="superuser", password="password"))
        response = self.client.get(reverse("aa_bb:index"))
        self.assertEqual(response.status_code, 200)

    def test_index_dropdown_options_recruiter_access(self):
        self.assertTrue(self.client.login(username="recruiter", password="password"))
        response = self.client.get(reverse("aa_bb:index"))
        self.assertEqual(response.status_code, 200)
        options = response.context['dropdown_options']
        self.assertNotIn("Member Char", options)
        self.assertIn("Guest Char", options)

    def test_available_cards_logic(self):
        # With URLs configured, both blacklist cards should be present
        cards = get_available_cards()
        keys = [c['key'] for c in cards]
        self.assertIn('alliance_bl', keys)
        self.assertIn('external_bl', keys)

        # Clear URLs and verify they are removed
        config = BigBrotherConfig.get_solo()
        config.alliance_blacklist_url = ""
        config.external_blacklist_url = ""
        config.save()

        cards = get_available_cards()
        keys = [c['key'] for c in cards]
        self.assertNotIn('alliance_bl', keys)
        self.assertNotIn('external_bl', keys)

        # Restore for other tests
        config.alliance_blacklist_url = "http://alliance.bl"
        config.external_blacklist_url = "http://external.bl"
        config.save()

    @patch('aa_bb.views.get_card_data')
    def test_load_card_ajax(self, mock_get_card_data):
        mock_get_card_data.return_value = ("<p>Test Card Content</p>", True)
        self.assertTrue(self.client.login(username="admin", password="password"))

        # Find index of a standard card (e.g., 'skills')
        cards = get_available_cards()
        skills_idx = next(i for i, c in enumerate(cards) if c['key'] == 'skills')

        url = reverse("aa_bb:load_card")
        response = self.client.get(url, {'option': 'Member Char', 'index': skills_idx})

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['title'], 'Skills')
        self.assertEqual(data['content'], "<p>Test Card Content</p>")
        self.assertTrue(data['status'])

    def test_load_card_invalid_params(self):
        self.assertTrue(self.client.login(username="admin", password="password"))
        url = reverse("aa_bb:load_card")

        # Missing parameters
        response = self.client.get(url)
        self.assertEqual(response.status_code, 400)

        # Invalid index
        response = self.client.get(url, {'option': 'Member Char', 'index': 999})
        self.assertEqual(response.status_code, 400)

        # Unknown character
        response = self.client.get(url, {'option': 'Nonexistent Char', 'index': 0})
        self.assertEqual(response.status_code, 404)

    def test_manual_modules_view(self):
        self.assertTrue(self.client.login(username="admin", password="password"))
        url = reverse("aa_bb:manual_modules")
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "faq/modules.html")
        self.assertIn('modules', response.context)

        # Verify some module names are present
        content = response.content.decode()
        self.assertIn("BigBrother Core Dashboard", content)
        self.assertIn("CorpBrother Dashboard", content)

