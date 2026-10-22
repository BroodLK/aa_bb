# Django
from django.contrib.auth.models import User
from django.test import TestCase

# Alliance Auth
from allianceauth.eveonline.models import EveCharacter

# AA BigBrother
from aa_bb.models import BigBrotherConfig
from aa_bb.tasks import BB_run_regular_updates


class TestMainCorporationOverride(TestCase):
    def test_manual_override_updates_main_corp_snapshot(self):
        cfg = BigBrotherConfig.get_solo()
        cfg.manual_main_corporation_override = True
        cfg.manual_main_corporation_id = 424242
        cfg.save()

        cfg.refresh_from_db()
        self.assertTrue(cfg.manual_main_corporation_override)
        self.assertEqual(cfg.manual_main_corporation_id, 424242)
        self.assertEqual(cfg.main_corporation_id, 424242)

    def test_regular_updates_do_not_overwrite_manual_main_corp(self):
        superuser = User.objects.create_superuser(
            username="admin",
            password="x",
            email="admin@example.com",
        )
        main_char = EveCharacter.objects.create(
            character_id=9000001,
            character_name="Updater Anchor",
            corporation_id=111111,
            corporation_name="Auto Corp",
            alliance_id=222222,
            alliance_name="Auto Alliance",
        )
        profile = superuser.profile
        profile.main_character = main_char
        profile.save()

        cfg = BigBrotherConfig.get_solo()
        cfg.is_active = False
        cfg.manual_main_corporation_override = True
        cfg.manual_main_corporation_id = 424242
        cfg.save()

        BB_run_regular_updates()

        cfg.refresh_from_db()
        self.assertEqual(cfg.main_corporation_id, 424242)
