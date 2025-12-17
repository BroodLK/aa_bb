"""Shared helpers for the reddit automation module."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import random
from dataclasses import dataclass
from typing import Optional

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from .models import BigBrotherConfig, BigBrotherRedditMessage, BigBrotherRedditSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RedditModuleStatus:
    messages_ready: bool
    praw_ready: bool
    api_credentials_ready: bool
    token_available: bool

    @property
    def fully_operational(self) -> bool:
        return (
            self.messages_ready
            and self.praw_ready
            and self.api_credentials_ready
            and self.token_available
        )


def praw_available() -> bool:
    return importlib.util.find_spec("praw") is not None


def reddit_messages_available() -> bool:
    return BigBrotherRedditMessage.objects.exists()


def reddit_app_configured(settings: BigBrotherRedditSettings) -> bool:
    required = [
        settings.reddit_client_id,
        settings.reddit_client_secret,
        settings.reddit_user_agent,
    ]
    return all(required)


def reddit_token_available(settings: BigBrotherRedditSettings) -> bool:
    return bool(settings.reddit_refresh_token)


def reddit_status(settings: Optional[BigBrotherRedditSettings] = None) -> RedditModuleStatus:
    settings = settings or BigBrotherRedditSettings.get_solo()
    return RedditModuleStatus(
        messages_ready=reddit_messages_available(),
        praw_ready=praw_available(),
        api_credentials_ready=reddit_app_configured(settings),
        token_available=reddit_token_available(settings),
    )


def import_praw():
    spec = importlib.util.find_spec("praw")
    if spec is None:
        raise RuntimeError("praw is not installed")
    return importlib.import_module("praw")


def get_reddit_client(settings: BigBrotherRedditSettings):
    praw = import_praw()
    missing = []
    for attr in [
        "reddit_client_id",
        "reddit_client_secret",
        "reddit_user_agent",
        "reddit_refresh_token",
    ]:
        if not getattr(settings, attr):
            missing.append(attr)
    if missing:
        raise RuntimeError(f"Missing reddit credentials: {', '.join(missing)}")
    return praw.Reddit(
        client_id=settings.reddit_client_id,
        client_secret=settings.reddit_client_secret,
        user_agent=settings.reddit_user_agent,
        refresh_token=settings.reddit_refresh_token,
    )


def pick_reddit_message() -> Optional[BigBrotherRedditMessage]:
    unsent = BigBrotherRedditMessage.objects.filter(used_in_cycle=False)
    if not unsent.exists():
        BigBrotherRedditMessage.objects.update(used_in_cycle=False)
        unsent = BigBrotherRedditMessage.objects.filter(used_in_cycle=False)
    if not unsent.exists():
        return None
    return random.choice(list(unsent))


def enough_time_since_last_post(settings: BigBrotherRedditSettings) -> bool:
    if settings.last_submission_at is None:
        return True
    delta = timezone.now() - settings.last_submission_at
    return delta.days >= max(1, settings.post_interval_days)


__all__ = [
    "RedditModuleStatus",
    "praw_available",
    "reddit_messages_available",
    "reddit_app_configured",
    "reddit_token_available",
    "reddit_status",
    "get_reddit_client",
    "pick_reddit_message",
    "enough_time_since_last_post",
]
