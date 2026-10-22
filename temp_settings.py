# AA BigBrother
from testauth.settings.local import *  # noqa: F401, F403

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
