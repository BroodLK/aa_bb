"""Celery tasks that back the reddit automation module."""

from __future__ import annotations

import logging
from datetime import datetime, timezone as datetime_timezone
from typing import Dict

from celery import shared_task
from django.db import OperationalError, ProgrammingError
from django.utils import timezone

from .app_settings import send_message
from .models import BigBrotherRedditSettings
from .reddit import (
    get_reddit_client,
    pick_reddit_message,
    enough_time_since_last_post,
)

logger = logging.getLogger(__name__)


def _render_template(template: str, context: Dict[str, str], fallback: str) -> str:
    """Format a template with context, falling back when keys are missing."""
    base = template or fallback
    try:
        return base.format(**context)
    except KeyError:
        return base


def _get_settings() -> BigBrotherRedditSettings | None:
    """Return the singleton reddit settings or None when unavailable."""
    try:
        return BigBrotherRedditSettings.get_solo()
    except (BigBrotherRedditSettings.DoesNotExist, OperationalError, ProgrammingError):
        logger.debug("Reddit settings are not available yet.")
        return None


@shared_task
def post_reddit_recruitment() -> str:
    """Pick a recruitment message and submit it to Reddit if the module allows it."""
    settings = _get_settings()
    if settings is None or not settings.enabled:  # Missing or disabled configuration.
        return "reddit module disabled"

    if not settings.last_submission_at:  # First post ever—just log informationally.
        logger.info("Reddit module will post its first message.")
    elif not enough_time_since_last_post(settings):  # Cooldown not yet expired.
        return "cooldown"

    message = pick_reddit_message()
    if message is None:  # No unused messages available to post.
        logger.warning("No reddit messages available for posting.")
        return "no-messages"

    try:
        reddit = get_reddit_client(settings)
    except RuntimeError as exc:
        logger.error("Cannot initialise PRAW client: %s", exc)
        return "praw-misconfigured"

    subreddit_name = settings.reddit_subreddit or "evejobs"
    try:
        submission = reddit.subreddit(subreddit_name).submit(
            title=message.title,
            selftext=message.content,
            send_replies=True,
        )
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to post reddit submission: %s", exc)
        return "reddit-error"

    message.used_in_cycle = True
    message.save(update_fields=["used_in_cycle"])

    permalink = getattr(submission, "permalink", "")
    full_permalink = f"https://www.reddit.com{permalink}" if permalink else getattr(submission, "url", "")

    settings.last_submission_id = getattr(submission, "id", "")
    settings.last_submission_permalink = full_permalink
    now = timezone.now()
    settings.last_submission_at = now
    settings.last_reply_checked_at = now
    settings.save(update_fields=[
        "last_submission_id",
        "last_submission_permalink",
        "last_submission_at",
        "last_reply_checked_at",
    ])

    webhook = settings.reddit_webhook
    if webhook:  # Only notify Discord when a hook has been configured.
        template = settings.reddit_webhook_message
        shortlink = getattr(submission, "shortlink", full_permalink)
        content = _render_template(
            template,
            {
                "title": getattr(submission, "title", message.title),
                "url": shortlink or full_permalink,
                "permalink": full_permalink,
                "subreddit": subreddit_name,
            },
            fallback="New reddit post published: {title}\n{url}",
        )
        send_message(content, hook=webhook)

    logger.info("Posted reddit submission %s", settings.last_submission_id)
    return "posted"


@shared_task
def monitor_reddit_replies() -> str:
    """Poll the last submission for replies and broadcast them via webhook."""
    settings = _get_settings()
    if (
        settings is None
        or not settings.enabled
        or not settings.last_submission_id
    ):
        # Without config/enabled flag/last submission context there is nothing to poll.
        return "idle"

    try:
        reddit = get_reddit_client(settings)
    except RuntimeError as exc:
        logger.error("Cannot initialise PRAW client: %s", exc)
        return "praw-misconfigured"

    try:
        submission = reddit.submission(id=settings.last_submission_id)
        submission.comments.replace_more(limit=0)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to load reddit comments: %s", exc)
        return "reddit-error"

    last_checked = settings.last_reply_checked_at or datetime.fromtimestamp(0, tz=datetime_timezone.utc)
    new_comments = []
    for comment in submission.comments.list():
        created = datetime.fromtimestamp(getattr(comment, "created_utc", 0), tz=datetime_timezone.utc)
        if created > last_checked:  # Only track replies newer than the checkpoint.
            new_comments.append((created, comment))

    if not new_comments:  # Update checkpoint even if nothing new to report.
        settings.last_reply_checked_at = timezone.now()
        settings.save(update_fields=["last_reply_checked_at"])
        return "no-updates"

    new_comments.sort(key=lambda item: item[0])
    for created, comment in new_comments:
        author_name = getattr(getattr(comment, "author", None), "name", "[deleted]")
        permalink = getattr(comment, "permalink", "")
        full_permalink = f"https://www.reddit.com{permalink}" if permalink else ""
        content = _render_template(
            settings.reply_message_template,
            {
                "author": author_name,
                "url": full_permalink,
                "created": created.isoformat(),
            },
            fallback="New reply by {author}: {url}",
        )
        send_message(content)

    settings.last_reply_checked_at = new_comments[-1][0].astimezone(timezone.utc)
    settings.save(update_fields=["last_reply_checked_at"])
    logger.info("Reported %s reddit replies", len(new_comments))
    return "reported"


__all__ = ["post_reddit_recruitment", "monitor_reddit_replies"]
