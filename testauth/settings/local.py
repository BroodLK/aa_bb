"""
Test settings
"""

# flake8: noqa

########################################################
# local.py settings
# Every setting in base.py can be overloaded by redefining it here.

# Third Party
from celery.schedules import crontab

from .base import *

PACKAGE = "aa_bb"

# modeltranslation must be first for eve_sde
INSTALLED_APPS = ["modeltranslation"] + INSTALLED_APPS

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/1.10/howto/static-files/
# STATICFILES_DIRS = [os.path.join(PROJECT_DIR, f"{PACKAGE}/static")]
STATICFILES_DIRS = [
    f"{PACKAGE}/static",
]

SITE_URL = "https://example.com"
CSRF_TRUSTED_ORIGINS = [SITE_URL]

DISCORD_BOT_TOKEN = "My_Dummy_Token"
# These are required for Django to function properly. Don't touch.
ROOT_URLCONF = "testauth.urls"
WSGI_APPLICATION = "testauth.wsgi.application"
SECRET_KEY = "t$@h+j#yqhmuy$x7$fkhytd&drajgfsb-6+j9pqn*vj0)gq&-2"

# This is where css/images will be placed for your webserver to read
STATIC_ROOT = "/var/www/testauth/static/"

# Change this to change the name of the auth site displayed
# in page titles and the site header.
SITE_NAME = "testauth"

# Change this to enable/disable debug mode, which displays
# useful error messages but can leak sensitive data.
DEBUG = False


LOGGING = False
NOTIFICATIONS_REFRESH_TIME = 30
NOTIFICATIONS_MAX_PER_USER = 50


# if os.environ.get("USE_MYSQL", True) is True:
#     DATABASES["default"] = {
#         "ENGINE": "django.db.backends.mysql",
#         "NAME": "tox_allianceauth",
#         "USER": os.environ.get("DB_USER", "user"),
#         "PASSWORD": os.environ.get("DB_PASSWORD", "password"),
#         "HOST": os.environ.get("DB_HOST", ""),
#         "PORT": os.environ.get("DB_PORT", ""),
#         "OPTIONS": {"charset": "utf8mb4"},
#         "TEST": {
#             "CHARSET": "utf8mb4",
#             "NAME": "test_tox_allianceauth",
#         },
#     }

# Add any additional apps to this list.
INSTALLED_APPS += [
    "eve_sde",
    "allianceauth.services.modules.discord",
    PACKAGE,
]

try:
    # Third Party
    import corptools

    INSTALLED_APPS.append("corptools")
except ImportError:
    pass

if "eve_sde" in INSTALLED_APPS:
    CELERYBEAT_SCHEDULE["EVE SDE :: Check for SDE Updates"] = {
        "task": "eve_sde.tasks.check_for_sde_updates",
        "schedule": crontab(minute="0", hour="12"),
        "apply_offset": True,
    }

# Capture request actors for admin log attribution.
MIDDLEWARE += ["aa_bb.middleware.AdminLogContextMiddleware"]

# By default, apps are prevented from having public views for security reasons.
# If you want to allow specific apps to have public views,
# you can put their names here (same name as in INSTALLED_APPS).
#
# Note:
#   » The format is the same as in INSTALLED_APPS
#   » The app developer must explicitly allow public views for his app
APPS_WITH_PUBLIC_VIEWS = []

# ------------------------------------------------------------------------------------ #
#
#                                  ESI Settings
#
# ------------------------------------------------------------------------------------ #
# Register an application at
# https://developers.eveonline.com for Authentication
# & API Access and fill out these settings.
# Be sure to set the callback URL
# to https://example.com/sso/callback
# substituting your domain for example.com
# Logging in to auth requires the publicData
# scope (can be overridden through the
# LOGIN_TOKEN_SCOPES setting).
# Other apps may require more (see their docs).
ESI_SSO_CLIENT_ID = "dummy"
ESI_SSO_CLIENT_SECRET = "dummy"
ESI_SSO_CALLBACK_URL = "http://localhost:8000"
ESI_USER_CONTACT_EMAIL = "dummy@example.net"


# ------------------------------------------------------------------------------------ #
#
#                                E-Mail Settings
#
# ------------------------------------------------------------------------------------ #
# By default, emails are validated before new users can log in.
# It's recommended to use a free service like SparkPost
# or Elastic Email to send email.
# Https://www.sparkpost.com/docs/integrations/django/
# https://elasticemail.com/resources/settings/smtp-api/
# Set the default from email to something like 'noreply@example.com'
# Email validation can be turned off by uncommenting the line below.
# This can break some services.
REGISTRATION_VERIFY_EMAIL = False
EMAIL_HOST = ""
EMAIL_PORT = 587
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
EMAIL_USE_TLS = True
DEFAULT_FROM_EMAIL = ""

#######################################
# Add any custom settings below here. #
#######################################

try:
    # Third Party
    import redis

    _r = redis.Redis(host="127.0.0.1", port=6379, socket_connect_timeout=0.2)
    _r.ping()
except Exception:
    # Standard Library
    from unittest.mock import MagicMock

    # Third Party
    import django_redis

    # Alliance Auth
    import allianceauth.utils.cache
    from allianceauth.services.modules.discord.discord_client.client import (
        DiscordClient,
    )

    _dummy_redis = MagicMock()
    django_redis.get_redis_connection = lambda alias="default": _dummy_redis
    allianceauth.utils.cache.get_redis_client = lambda: _dummy_redis
    _orig_dc_init = DiscordClient.__init__

    def _safe_dc_init(self, *args, **kwargs):
        kwargs["redis"] = _dummy_redis
        _orig_dc_init(self, *args, **kwargs)

    DiscordClient.__init__ = _safe_dc_init
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "unique-snowflake",
        }
    }

    # Django
    from django.core.cache.backends.locmem import LocMemCache

    _orig_locmem_set = LocMemCache.set

    def _safe_locmem_set(self, key, value, timeout=None, version=None, **kwargs):
        if kwargs.get("nx"):
            return self.add(key, value, timeout=timeout, version=version)
        return _orig_locmem_set(self, key, value, timeout=timeout, version=version)

    LocMemCache.set = _safe_locmem_set

CELERY_ALWAYS_EAGER = True
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
BROKER_URL = "memory://"
CELERY_BROKER_URL = "memory://"
