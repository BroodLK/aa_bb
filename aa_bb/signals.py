"""
Django signal handlers used by BigBrother.

Currently:
1. When the singleton config is saved, Celery message tasks stay in sync.
2. When a character ownership is deleted, optionally open a compliance ticket.
3. Admin/Auth audit logging for state/group/discord/admin events.
"""

# Standard Library
from datetime import timedelta

# Django
from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Group, User
from django.db.models.signals import m2m_changed, post_save, pre_delete, pre_save
from django.db.utils import OperationalError, ProgrammingError
from django.dispatch import receiver
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership, UserProfile
from allianceauth.services.hooks import get_extension_logger

from .models import AdminLogEntry, BigBrotherConfig, TicketToolConfig
from .request_context import get_request_context
from .tasks_cb import BB_register_message_tasks

logger = get_extension_logger(__name__)

TOKEN_SCOPE_LOG_DEBOUNCE_SECONDS = getattr(
    settings,
    "AA_BB_TOKEN_SCOPE_LOG_DEBOUNCE_SECONDS",
    5,
)

try:
    LOGENTRY_ADDITION = LogEntry.ActionFlag.ADDITION
    LOGENTRY_CHANGE = LogEntry.ActionFlag.CHANGE
    LOGENTRY_DELETION = LogEntry.ActionFlag.DELETION
except Exception:
    LOGENTRY_ADDITION = getattr(LogEntry, "ADDITION", 1)
    LOGENTRY_CHANGE = getattr(LogEntry, "CHANGE", 2)
    LOGENTRY_DELETION = getattr(LogEntry, "DELETION", 3)


try:
    # Alliance Auth
    from allianceauth.services.modules.discord.models import DiscordUser
except ImportError:
    DiscordUser = None

try:
    # Alliance Auth
    from allianceauth.groupmanagement.models import GroupRequest, RequestLog
except ImportError:
    GroupRequest = None
    RequestLog = None

try:
    # Alliance Auth
    from allianceauth.authentication.models import OwnershipRecord, State
except ImportError:
    OwnershipRecord = None
    State = None

try:
    # Alliance Auth
    from esi.models import Token
except ImportError:
    Token = None

try:
    # Third Party
    from celery import signals as celery_signals
except Exception:
    celery_signals = None


def _get_current_task_context():
    try:
        # Third Party
        from celery import current_task
    except Exception:
        return None, None
    try:
        if current_task is None:
            return None, None
        task_name = getattr(current_task, "name", None) or getattr(
            getattr(current_task, "request", None), "task", None
        )
        task_id = getattr(getattr(current_task, "request", None), "id", None)
        return task_name, task_id
    except Exception:
        return None, None


def _is_discord_update_groups_task_name(task_name):
    name = (task_name or "").lower()
    return bool(name) and ("discord" in name and "update_groups" in name)


def log_admin_event(
    category,
    action,
    actor=None,
    target_user=None,
    target_label="",
    message="",
    reason="",
    source="",
    task_name="",
    metadata=None,
):
    """Best-effort audit logger; avoids raising during migrations/startup."""
    if metadata is None:
        metadata = {}
    else:
        metadata = dict(metadata)
    inferred_task_name, inferred_task_id = _get_current_task_context()
    context_user, context_meta = get_request_context()
    if actor is None and context_user is not None:
        actor = context_user
    if not task_name:
        task_name = inferred_task_name or ""
    if not source:
        source = "task" if task_name else "signal"
    if not target_label and target_user is not None:
        target_label = getattr(target_user, "username", "") or ""
    if inferred_task_id and "task_id" not in metadata:
        metadata["task_id"] = inferred_task_id
    if context_meta and "request" not in metadata:
        metadata["request"] = context_meta
    try:
        AdminLogEntry.objects.create(
            category=category,
            action=action,
            source=source or "",
            task_name=task_name or "",
            reason=reason or "",
            actor=actor,
            target_user=target_user,
            target_label=target_label or "",
            message=message or "",
            metadata=metadata,
        )
    except (OperationalError, ProgrammingError):
        return
    except Exception as e:
        logger.error("Failed to write admin log entry: %s", e, exc_info=True)


def _get_latest_discord_log_entry(user_id):
    if not user_id:
        return None
    try:
        return (
            AdminLogEntry.objects.filter(category=AdminLogEntry.CATEGORY_DISCORD, target_user_id=user_id)
            .order_by("-created_at")
            .first()
        )
    except (OperationalError, ProgrammingError):
        return None
    except Exception as e:
        logger.error("Failed to fetch latest discord log entry: %s", e, exc_info=True)
        return None


def _extract_discord_id_from_log(entry):
    if not entry:
        return None
    meta = getattr(entry, "metadata", None)
    if isinstance(meta, dict):
        discord_id = meta.get("discord_id")
        if discord_id:
            return str(discord_id)
    if entry.target_label:
        return str(entry.target_label)
    return None


def _send_discord_relink_alert(user, old_discord_id, new_discord_id):
    if not user or not old_discord_id or not new_discord_id:
        return
    try:
        if old_discord_id != new_discord_id:
            from .app_settings import send_message

            username = getattr(user, "username", "unknown")
            content = (
                "ALERT: Discord relink detected: "
                f"{username} (user_id={user.id}) "
                f"unlinked {old_discord_id} and linked a different discord ID {new_discord_id}."
            )
            send_message(content)
    except (OperationalError, ProgrammingError):
        return
    except Exception as e:
        logger.error("Failed to send discord relink alert: %s", e, exc_info=True)


def _get_token_created_at(token):
    for attr in ("created", "created_at", "created_on", "created_date"):
        value = getattr(token, attr, None)
        if value:
            return value
    return None


def _format_age_seconds(age_seconds):
    if age_seconds is None:
        return None
    try:
        age_seconds = float(age_seconds)
    except (TypeError, ValueError):
        return None
    if age_seconds < 60:
        return f"{int(age_seconds)}s"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h"
    return f"{int(age_seconds // 86400)}d"


def _infer_token_reason_hint(request_meta, task_name=""):
    request_meta = request_meta or {}
    path = (request_meta.get("path") or "").strip()
    if path:
        return path
    if task_name:
        return f"task:{task_name}"
    return None


def _extract_discord_role_changes(result):
    added = []
    removed = []
    if isinstance(result, dict):
        added = result.get("added") or result.get("roles_added") or result.get("add") or []
        removed = result.get("removed") or result.get("roles_removed") or result.get("remove") or []
        if not added and not removed:
            before = result.get("before") or result.get("old") or []
            after = result.get("after") or result.get("new") or []
            if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
                before_set = set(str(v) for v in before)
                after_set = set(str(v) for v in after)
                added = sorted(after_set - before_set)
                removed = sorted(before_set - after_set)
    elif isinstance(result, (list, tuple)) and len(result) == 2:
        added, removed = result
    added = [str(v) for v in (added or []) if v is not None]
    removed = [str(v) for v in (removed or []) if v is not None]
    return added, removed


@receiver(post_save, sender=BigBrotherConfig)
@receiver(post_save, sender=TicketToolConfig)
def trigger_task_sync(sender, instance, **kwargs):
    """When the config changes, make sure Celery schedules match the DB."""
    BB_register_message_tasks.delay()


@receiver(pre_delete, sender=CharacterOwnership)
def removed_character(sender, instance, **kwargs):
    """
    If the ticket tool is monitoring “character removed” events, raise a ticket
    any time Auth loses access to one of the pilot’s characters.
    """
    if not TicketToolConfig.get_solo().char_removed_enabled:
        return
    try:
        character = instance.character
        bb_cfg = BigBrotherConfig.get_solo()
        member_states = bb_cfg.bb_member_states.all()
        if instance.user.profile.state not in member_states:
            return

        if bb_cfg.limit_to_main_corp:
            # Check if the user's main character belongs to the primary corporation
            profile = getattr(instance.user, "profile", None)
            main_char = getattr(profile, "main_character", None) if profile else None
            if not main_char or main_char.corporation_id != bb_cfg.main_corporation_id:
                return

        from .tasks_tickets import ensure_ticket

        ensure_ticket(instance.user, "char_removed", details=str(character))

    except Exception as e:
        logger.error("✅  [AA-BB] - [Signals] - Failed to create character-removed ticket: %s", e)


@receiver(pre_delete, sender=CharacterOwnership)
def audit_character_removed(sender, instance, **kwargs):
    """Log when a character is removed from auth ownership."""
    try:
        character = instance.character
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="character_removed",
            target_user=instance.user,
            target_label=getattr(character, "character_name", str(character)),
            message="Character removed from auth",
            reason="ownership_removed",
            source="auth",
            metadata={"character_id": getattr(character, "character_id", None)},
        )
    except Exception as e:
        logger.error("Failed to log character remove event: %s", e, exc_info=True)


@receiver(post_save, sender=CharacterOwnership)
def added_character(sender, instance, created, **kwargs):
    """Log when a character is added to auth ownership."""
    if not created:
        return
    try:
        character = instance.character
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="character_added",
            target_user=instance.user,
            target_label=getattr(character, "character_name", str(character)),
            message="Character added to auth",
            reason="ownership_added",
            source="auth",
            metadata={"character_id": getattr(character, "character_id", None)},
        )
    except Exception as e:
        logger.error("Failed to log character add event: %s", e, exc_info=True)


@receiver(pre_save, sender=UserProfile)
def cache_profile_state(sender, instance, **kwargs):
    """Capture old profile values for audit logging."""
    if not instance.pk:
        instance._bb_prev_state_id = None
        instance._bb_prev_state_name = None
        instance._bb_prev_main_character_id = None
        instance._bb_prev_main_character_name = None
        return
    try:
        prev = UserProfile.objects.select_related("state", "main_character").get(pk=instance.pk)
        instance._bb_prev_state_id = prev.state_id
        instance._bb_prev_state_name = prev.state.name if prev.state else None
        instance._bb_prev_main_character_id = prev.main_character_id
        instance._bb_prev_main_character_name = prev.main_character.character_name if prev.main_character else None
    except UserProfile.DoesNotExist:
        instance._bb_prev_state_id = None
        instance._bb_prev_state_name = None
        instance._bb_prev_main_character_id = None
        instance._bb_prev_main_character_name = None


@receiver(post_save, sender=UserProfile)
def log_profile_changes(sender, instance, created, **kwargs):
    """Log state and main-character changes."""
    if created:
        return

    prev_state_id = getattr(instance, "_bb_prev_state_id", None)
    if prev_state_id is not None and prev_state_id != instance.state_id:
        old_name = getattr(instance, "_bb_prev_state_name", None)
        new_name = instance.state.name if instance.state else None
        log_admin_event(
            category=AdminLogEntry.CATEGORY_STATE,
            action="state_changed",
            target_user=instance.user,
            message=f"State changed from {old_name} to {new_name}",
            reason="state_changed",
            source="auth",
            metadata={"old_state": old_name, "new_state": new_name},
        )

    prev_main_id = getattr(instance, "_bb_prev_main_character_id", None)
    if prev_main_id is not None and prev_main_id != instance.main_character_id:
        old_char = getattr(instance, "_bb_prev_main_character_name", None)
        new_char = instance.main_character.character_name if instance.main_character else None
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="main_character_changed",
            target_user=instance.user,
            message=f"Main character changed from {old_char} to {new_char}",
            reason="main_character_changed",
            source="auth",
            metadata={"old_main_character": old_char, "new_main_character": new_char},
        )


@receiver(pre_save, sender=User)
def cache_user_state(sender, instance, **kwargs):
    """Capture user fields to detect changes."""
    if not instance.pk:
        instance._bb_prev_user = None
        return
    try:
        instance._bb_prev_user = User.objects.get(pk=instance.pk)
    except User.DoesNotExist:
        instance._bb_prev_user = None


@receiver(post_save, sender=User)
def log_user_changes(sender, instance, created, **kwargs):
    """Log core user changes."""
    if created:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="user_created",
            target_user=instance,
            message="User created",
            reason="user_created",
            source="auth",
        )
        return

    prev = getattr(instance, "_bb_prev_user", None)
    if not prev:
        return

    if prev.is_active != instance.is_active:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="user_activated" if instance.is_active else "user_deactivated",
            target_user=instance,
            message=f"User {'activated' if instance.is_active else 'deactivated'}",
            reason="user_status_changed",
            source="auth",
            metadata={"old": prev.is_active, "new": instance.is_active},
        )
    if prev.is_staff != instance.is_staff:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="staff_granted" if instance.is_staff else "staff_revoked",
            target_user=instance,
            message=f"Staff status {'granted' if instance.is_staff else 'revoked'}",
            reason="staff_status_changed",
            source="auth",
            metadata={"old": prev.is_staff, "new": instance.is_staff},
        )
    if prev.is_superuser != instance.is_superuser:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="superuser_granted" if instance.is_superuser else "superuser_revoked",
            target_user=instance,
            message=f"Superuser status {'granted' if instance.is_superuser else 'revoked'}",
            reason="superuser_status_changed",
            source="auth",
            metadata={"old": prev.is_superuser, "new": instance.is_superuser},
        )
    if prev.username != instance.username:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="username_changed",
            target_user=instance,
            message=f"Username changed from {prev.username} to {instance.username}",
            reason="username_changed",
            source="auth",
            metadata={"old": prev.username, "new": instance.username},
        )
    if prev.email != instance.email:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="email_changed",
            target_user=instance,
            message=f"Email changed from {prev.email} to {instance.email}",
            reason="email_changed",
            source="auth",
            metadata={"old": prev.email, "new": instance.email},
        )


@receiver(pre_delete, sender=User)
def log_user_deleted(sender, instance, **kwargs):
    """Log when a user is deleted."""
    log_admin_event(
        category=AdminLogEntry.CATEGORY_AUTH,
        action="user_deleted",
        target_user=instance,
        target_label=instance.username,
        message="User deleted",
        reason="user_deleted",
        source="auth",
    )


@receiver(m2m_changed, sender=User.groups.through)
def log_group_changes(sender, instance, action, pk_set, **kwargs):
    """Log user group membership changes."""
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    task_name, _task_id = _get_current_task_context()
    is_discord_task = _is_discord_update_groups_task_name(task_name)
    source = "discord" if is_discord_task else "auth"
    reason = "discord_update_groups" if is_discord_task else "group_membership_changed"

    if action == "post_clear":
        log_admin_event(
            category=AdminLogEntry.CATEGORY_GROUP,
            action="groups_cleared",
            target_user=instance,
            message="All groups cleared for user",
            reason="groups_cleared",
            source=source,
            task_name=task_name or "",
        )
        return

    groups = Group.objects.filter(pk__in=pk_set) if pk_set else []
    for group in groups:
        log_admin_event(
            category=AdminLogEntry.CATEGORY_GROUP,
            action="group_added" if action == "post_add" else "group_removed",
            target_user=instance,
            target_label=group.name,
            message=f"Group {'added' if action == 'post_add' else 'removed'}: {group.name}",
            reason=reason,
            source=source,
            task_name=task_name or "",
            metadata={"group_id": group.id, "via_task": task_name or ""},
        )


def _resolve_target_user_from_log_entry(entry):
    """Attempt to find a target user based on admin LogEntry metadata."""
    if not entry.content_type or not entry.object_id:
        return None
    object_id = entry.object_id
    try:
        object_id_int = int(object_id)
    except (TypeError, ValueError):
        object_id_int = None

    app_label = entry.content_type.app_label
    model = entry.content_type.model

    if app_label == "auth" and model == "user" and object_id_int:
        return User.objects.filter(pk=object_id_int).first()
    if app_label == "authentication" and model == "userprofile" and object_id_int:
        try:
            return UserProfile.objects.select_related("user").get(pk=object_id_int).user
        except UserProfile.DoesNotExist:
            return None
    if app_label == "authentication" and model == "characterownership" and object_id_int:
        try:
            return CharacterOwnership.objects.select_related("user").get(pk=object_id_int).user
        except CharacterOwnership.DoesNotExist:
            return None

    return None


@receiver(post_save, sender=LogEntry)
def log_admin_actions(sender, instance, created, **kwargs):
    """Record Django admin actions."""
    if not created:
        return

    if instance.content_type:
        if instance.content_type.app_label == "aa_bb" and instance.content_type.model == "adminlogentry":
            return
        if instance.content_type.app_label == "admin" and instance.content_type.model == "logentry":
            return

    action_map = {
        LOGENTRY_ADDITION: "admin_add",
        LOGENTRY_CHANGE: "admin_change",
        LOGENTRY_DELETION: "admin_delete",
    }
    action = action_map.get(instance.action_flag, "admin_action")
    target_user = _resolve_target_user_from_log_entry(instance)

    log_admin_event(
        category=AdminLogEntry.CATEGORY_ADMIN,
        action=action,
        actor=instance.user,
        target_user=target_user,
        target_label=instance.object_repr,
        message=instance.change_message or instance.object_repr,
        reason=instance.change_message or "",
        source="admin",
        metadata={
            "content_type": str(instance.content_type) if instance.content_type else None,
            "object_id": instance.object_id,
            "action_flag": instance.action_flag,
        },
    )


if DiscordUser is not None:

    @receiver(post_save, sender=DiscordUser)
    def log_discord_user_change(sender, instance, created, **kwargs):
        """Log when a Discord account is linked or updated."""
        discord_id = getattr(instance, "uid", None) or getattr(instance, "discord_id", None)
        prior_entry = _get_latest_discord_log_entry(getattr(instance, "user_id", None))
        prior_unlink = prior_entry and prior_entry.action == "discord_unlinked"
        prior_discord_id = _extract_discord_id_from_log(prior_entry) if prior_unlink else None
        new_discord_id = str(discord_id) if discord_id else None
        should_alert = prior_unlink and prior_discord_id and new_discord_id and prior_discord_id != new_discord_id
        log_admin_event(
            category=AdminLogEntry.CATEGORY_DISCORD,
            action="discord_linked" if created else "discord_updated",
            target_user=instance.user,
            target_label=str(discord_id) if discord_id else "",
            message="Discord account linked" if created else "Discord account updated",
            reason="discord_linked" if created else "discord_updated",
            source="discord",
            metadata={"discord_id": discord_id},
        )
        if should_alert:
            _send_discord_relink_alert(instance.user, prior_discord_id, new_discord_id)

    @receiver(pre_delete, sender=DiscordUser)
    def log_discord_user_unlink(sender, instance, **kwargs):
        """Log when a Discord account is unlinked."""
        discord_id = getattr(instance, "uid", None) or getattr(instance, "discord_id", None)
        log_admin_event(
            category=AdminLogEntry.CATEGORY_DISCORD,
            action="discord_unlinked",
            target_user=instance.user,
            target_label=str(discord_id) if discord_id else "",
            message="Discord account unlinked",
            reason="discord_unlinked",
            source="discord",
            metadata={"discord_id": discord_id},
        )


def _m2m_meta(pk_set):
    if not pk_set:
        return {"count": 0, "sample_ids": []}
    ids = sorted(list(pk_set))
    return {"count": len(ids), "sample_ids": ids[:20]}


def _merge_recent_token_scopes_log(instance, message, reason, metadata):
    if not instance or not instance.user_id:
        return False
    window_seconds = TOKEN_SCOPE_LOG_DEBOUNCE_SECONDS
    if not window_seconds or window_seconds <= 0:
        return False
    since = timezone.now() - timedelta(seconds=window_seconds)
    try:
        qs = AdminLogEntry.objects.filter(
            action="token_scopes_updated",
            source="esi",
            target_user_id=instance.user_id,
            created_at__gte=since,
        )
        target_label = instance.character_name or ""
        if target_label:
            qs = qs.filter(target_label=target_label)
        try:
            qs = qs.filter(metadata__token_id=instance.pk)
        except Exception:
            pass
        entry = qs.order_by("-created_at").first()
        if not entry:
            return False
        entry.message = message
        entry.reason = reason
        entry.metadata = metadata
        if target_label:
            entry.target_label = target_label
        entry.target_user = instance.user
        entry.actor = instance.user
        entry.source = "esi"
        entry.action = "token_scopes_updated"
        entry.save(
            update_fields=[
                "message",
                "reason",
                "metadata",
                "target_label",
                "target_user",
                "actor",
                "source",
                "action",
            ]
        )
        return True
    except Exception as e:
        logger.error("Failed to merge token scope log entry: %s", e, exc_info=True)
        return False


if Token is not None:

    @receiver(post_save, sender=Token)
    def log_token_created(sender, instance, created, **kwargs):
        """Log when an ESI token is created."""
        if not created:
            return
        _ctx_user, request_meta = get_request_context()
        request_meta = request_meta or {}
        task_name, _task_id = _get_current_task_context()
        reason_hint = _infer_token_reason_hint(request_meta, task_name)
        scopes = []
        try:
            scopes = sorted(s.name for s in instance.scopes.all())
        except Exception:
            scopes = []
        message_parts = ["ESI token created"]
        if reason_hint:
            message_parts.append(f"via {reason_hint}")
        if not scopes:
            message_parts.append("(scopes pending)")
        else:
            message_parts.append(f"(scopes {len(scopes)})")
        category = AdminLogEntry.CATEGORY_AUTH if scopes else AdminLogEntry.CATEGORY_SYSTEM
        log_admin_event(
            category=category,
            action="token_created",
            actor=instance.user,
            target_user=instance.user,
            target_label=instance.character_name,
            message=" ".join(message_parts),
            reason="token_created",
            source="esi",
            metadata={
                "character_id": instance.character_id,
                "token_type": instance.token_type,
                "scopes": scopes,
                "scopes_count": len(scopes),
                "scopes_empty": not scopes,
                "reason_hint": reason_hint,
                "request_path": request_meta.get("path"),
                "request_method": request_meta.get("method"),
                "request_ip": request_meta.get("ip"),
            },
        )

    @receiver(pre_delete, sender=Token)
    def log_token_deleted(sender, instance, **kwargs):
        """Log when an ESI token is deleted."""
        _ctx_user, request_meta = get_request_context()
        request_meta = request_meta or {}
        task_name, _task_id = _get_current_task_context()
        reason_hint = _infer_token_reason_hint(request_meta, task_name)
        scopes = []
        try:
            scopes = sorted(s.name for s in instance.scopes.all())
        except Exception:
            scopes = []
        created_at = _get_token_created_at(instance)
        age_seconds = None
        age_human = None
        if created_at:
            if timezone.is_naive(created_at):
                created_at = timezone.make_aware(created_at, timezone.get_current_timezone())
            age_seconds = (timezone.now() - created_at).total_seconds()
            age_human = _format_age_seconds(age_seconds)
        message_parts = ["ESI token deleted"]
        if scopes:
            message_parts.append(f"(scopes {len(scopes)})")
        else:
            message_parts.append("(scopes none)")
        if age_human:
            message_parts.append(f"(age {age_human})")
        if reason_hint:
            message_parts.append(f"via {reason_hint}")
        category = AdminLogEntry.CATEGORY_AUTH if scopes else AdminLogEntry.CATEGORY_SYSTEM
        log_admin_event(
            category=category,
            action="token_deleted",
            actor=instance.user,
            target_user=instance.user,
            target_label=instance.character_name,
            message=" ".join(message_parts),
            reason="token_deleted",
            source="esi",
            metadata={
                "character_id": instance.character_id,
                "token_type": instance.token_type,
                "scopes": scopes,
                "scopes_count": len(scopes),
                "scopes_empty": not scopes,
                "age_seconds": age_seconds,
                "age_human": age_human,
                "reason_hint": reason_hint,
                "request_path": request_meta.get("path"),
                "request_method": request_meta.get("method"),
                "request_ip": request_meta.get("ip"),
            },
        )

    @receiver(m2m_changed, sender=Token.scopes.through)
    def log_token_scopes_changed(sender, instance, action, pk_set, **kwargs):
        """Log when ESI token scopes are attached or removed."""
        if action not in ("post_add", "post_remove", "post_clear"):
            return
        scopes = []
        try:
            scopes = sorted(s.name for s in instance.scopes.all())
        except Exception:
            scopes = []
        message = f"ESI token scopes {action.replace('post_', '')}"
        reason = f"token_scopes_{action}"
        metadata = {
            "token_id": instance.pk,
            "character_id": instance.character_id,
            "token_type": instance.token_type,
            "scopes": scopes,
            "scopes_count": len(scopes),
            "scopes_empty": not scopes,
            "scope_change_action": action,
        }
        if _merge_recent_token_scopes_log(instance, message, reason, metadata):
            return
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="token_scopes_updated",
            actor=instance.user,
            target_user=instance.user,
            target_label=instance.character_name,
            message=message,
            reason=reason,
            source="esi",
            metadata=metadata,
        )


if OwnershipRecord is not None:

    @receiver(post_save, sender=OwnershipRecord)
    def log_ownership_record_created(sender, instance, created, **kwargs):
        """Log when an ownership record is created."""
        if not created:
            return
        log_admin_event(
            category=AdminLogEntry.CATEGORY_AUTH,
            action="ownership_record_created",
            target_user=instance.user,
            target_label=getattr(instance.character, "character_name", str(instance.character)),
            message="Ownership record created",
            reason="ownership_record_created",
            source="auth",
            metadata={
                "character_id": instance.character_id,
                "owner_hash": instance.owner_hash,
            },
        )


if State is not None:

    @receiver(post_save, sender=State)
    def log_state_saved(sender, instance, created, **kwargs):
        """Log when a state definition is created or updated."""
        log_admin_event(
            category=AdminLogEntry.CATEGORY_ADMIN,
            action="state_created" if created else "state_updated",
            target_label=instance.name,
            message="State definition saved",
            reason="state_saved",
            source="auth",
            metadata={"state_id": instance.id},
        )

    if hasattr(State, "member_characters"):

        @receiver(m2m_changed, sender=State.member_characters.through)
        def log_state_member_characters_changed(sender, instance, action, pk_set, **kwargs):
            if action not in ("post_add", "post_remove", "post_clear"):
                return
            log_admin_event(
                category=AdminLogEntry.CATEGORY_ADMIN,
                action="state_member_characters_changed",
                target_label=instance.name,
                message="State member characters updated",
                reason=f"member_characters_{action}",
                source="auth",
                metadata=_m2m_meta(pk_set),
            )

    if hasattr(State, "member_corporations"):

        @receiver(m2m_changed, sender=State.member_corporations.through)
        def log_state_member_corporations_changed(sender, instance, action, pk_set, **kwargs):
            if action not in ("post_add", "post_remove", "post_clear"):
                return
            log_admin_event(
                category=AdminLogEntry.CATEGORY_ADMIN,
                action="state_member_corporations_changed",
                target_label=instance.name,
                message="State member corporations updated",
                reason=f"member_corporations_{action}",
                source="auth",
                metadata=_m2m_meta(pk_set),
            )

    if hasattr(State, "member_alliances"):

        @receiver(m2m_changed, sender=State.member_alliances.through)
        def log_state_member_alliances_changed(sender, instance, action, pk_set, **kwargs):
            if action not in ("post_add", "post_remove", "post_clear"):
                return
            log_admin_event(
                category=AdminLogEntry.CATEGORY_ADMIN,
                action="state_member_alliances_changed",
                target_label=instance.name,
                message="State member alliances updated",
                reason=f"member_alliances_{action}",
                source="auth",
                metadata=_m2m_meta(pk_set),
            )

    if hasattr(State, "member_factions"):

        @receiver(m2m_changed, sender=State.member_factions.through)
        def log_state_member_factions_changed(sender, instance, action, pk_set, **kwargs):
            if action not in ("post_add", "post_remove", "post_clear"):
                return
            log_admin_event(
                category=AdminLogEntry.CATEGORY_ADMIN,
                action="state_member_factions_changed",
                target_label=instance.name,
                message="State member factions updated",
                reason=f"member_factions_{action}",
                source="auth",
                metadata=_m2m_meta(pk_set),
            )


if GroupRequest is not None:

    @receiver(post_save, sender=GroupRequest)
    def log_group_request_created(sender, instance, created, **kwargs):
        """Log when a user applies to join/leave a group."""
        if not created:
            return
        action = "group_leave_request_created" if instance.leave_request else "group_join_request_created"
        log_admin_event(
            category=AdminLogEntry.CATEGORY_GROUP,
            action=action,
            actor=instance.user,
            target_user=instance.user,
            target_label=instance.group.name,
            message="Group request created",
            reason=action,
            source="groupmanagement",
            metadata={
                "group_id": instance.group_id,
                "leave_request": bool(instance.leave_request),
                "group_request_id": instance.pk,
            },
        )

    @receiver(pre_delete, sender=GroupRequest)
    def log_group_request_cancelled(sender, instance, **kwargs):
        """Log when a user cancels their own request (best-effort)."""
        actor, _meta = get_request_context()
        if actor and instance.user_id == actor.id:
            action = "group_leave_request_cancelled" if instance.leave_request else "group_join_request_cancelled"
            log_admin_event(
                category=AdminLogEntry.CATEGORY_GROUP,
                action=action,
                actor=actor,
                target_user=instance.user,
                target_label=instance.group.name,
                message="Group request cancelled",
                reason=action,
                source="groupmanagement",
                metadata={
                    "group_id": instance.group_id,
                    "leave_request": bool(instance.leave_request),
                    "group_request_id": instance.pk,
                },
            )


if RequestLog is not None:

    def _requestlog_requestor(info):
        if not info:
            return None
        if ":" not in info:
            return info
        return info.split(":", 1)[0]

    def _requestlog_type_label(request_type):
        if request_type is True:
            return "leave"
        if request_type is False:
            return "join"
        return "removed"

    @receiver(post_save, sender=RequestLog)
    def log_group_request_processed(sender, instance, created, **kwargs):
        """Log when a group request is accepted or rejected."""
        if not created:
            return
        type_label = _requestlog_type_label(instance.request_type)
        action_label = "accepted" if instance.action else "rejected"
        action = f"group_request_{type_label}_{action_label}"
        requestor_name = _requestlog_requestor(instance.request_info)
        target_user = None
        if requestor_name:
            target_user = User.objects.filter(username=requestor_name).first()

        log_admin_event(
            category=AdminLogEntry.CATEGORY_GROUP,
            action=action,
            actor=instance.request_actor,
            target_user=target_user,
            target_label=instance.group.name,
            message="Group request processed",
            reason=action,
            source="groupmanagement",
            metadata={
                "request_type": instance.request_type,
                "action": instance.action,
                "request_info": instance.request_info,
                "group_id": instance.group_id,
                "request_log_id": instance.pk,
            },
        )


if celery_signals is not None:

    def _is_discord_update_groups_task(sender):
        name = getattr(sender, "name", "") or ""
        return _is_discord_update_groups_task_name(name)

    @celery_signals.task_success.connect
    def log_discord_task_success(sender=None, result=None, args=None, kwargs=None, **extras):
        if not sender or not _is_discord_update_groups_task(sender):
            return
        user_pk = None
        if args and len(args) > 0:
            user_pk = args[0]
        if kwargs and "user_pk" in kwargs:
            user_pk = kwargs.get("user_pk")
        target_user = User.objects.filter(pk=user_pk).first() if user_pk else None
        task_name = getattr(sender, "name", "")
        added, removed = _extract_discord_role_changes(result)
        if added or removed:
            for role in added:
                log_admin_event(
                    category=AdminLogEntry.CATEGORY_DISCORD,
                    action="discord_role_added",
                    target_user=target_user,
                    target_label=role,
                    message=f"Discord role added: {role}",
                    reason="discord_update_groups_success",
                    source="discord",
                    task_name=task_name,
                    metadata={
                        "role": role,
                        "change": "added",
                        "user_pk": user_pk,
                    },
                )
            for role in removed:
                log_admin_event(
                    category=AdminLogEntry.CATEGORY_DISCORD,
                    action="discord_role_removed",
                    target_user=target_user,
                    target_label=role,
                    message=f"Discord role removed: {role}",
                    reason="discord_update_groups_success",
                    source="discord",
                    task_name=task_name,
                    metadata={
                        "role": role,
                        "change": "removed",
                        "user_pk": user_pk,
                    },
                )
        else:
            log_admin_event(
                category=AdminLogEntry.CATEGORY_DISCORD,
                action="discord_roles_updated",
                target_user=target_user,
                target_label=getattr(target_user, "username", "") if target_user else "",
                message="Discord roles update completed (no changes detected)",
                reason="discord_update_groups_success",
                source="discord",
                task_name=task_name,
                metadata={
                    "result": result,
                    "user_pk": user_pk,
                },
            )

    @celery_signals.task_failure.connect
    def log_discord_task_failure(sender=None, exception=None, args=None, kwargs=None, **extras):
        if not sender or not _is_discord_update_groups_task(sender):
            return
        user_pk = None
        if args and len(args) > 0:
            user_pk = args[0]
        if kwargs and "user_pk" in kwargs:
            user_pk = kwargs.get("user_pk")
        target_user = User.objects.filter(pk=user_pk).first() if user_pk else None
        log_admin_event(
            category=AdminLogEntry.CATEGORY_DISCORD,
            action="discord_roles_update_failed",
            target_user=target_user,
            target_label=getattr(target_user, "username", "") if target_user else "",
            message="Discord roles update failed",
            reason="discord_update_groups_failed",
            source="discord",
            task_name=getattr(sender, "name", ""),
            metadata={
                "error": str(exception) if exception else "",
                "user_pk": user_pk,
            },
        )
