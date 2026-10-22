"""Regression tests for aa_bb app bootstrap imports."""

# Standard Library
import builtins
import importlib
from unittest import TestCase
from unittest.mock import patch


class TestAaBbAppsModuleImport(TestCase):
    def test_reloading_apps_module_does_not_import_allianceauth_hooks(self):
        module = importlib.import_module("aa_bb.apps")
        real_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "allianceauth.services.hooks":
                raise AssertionError(
                    "aa_bb.apps imported allianceauth.services.hooks during module import"
                )
            return real_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=guarded_import):
            reloaded = importlib.reload(module)

        self.assertEqual(reloaded.AaBbConfig.name, "aa_bb")
