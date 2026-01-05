"""
Discord ticket helper utilities used by BigBrother.

The functions here are called from Celery tasks as well as slash commands to
create/rebalance compliance ticket channels.
"""

import re
import logging

from allianceauth.authentication.models import UserProfile

from django.db import transaction
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

try:
    import discord
except ImportError:
    class discord:
        class Message: pass
        class Embed: pass
        class Color:
            @classmethod
            def from_rgb(cls, *args): pass
            @classmethod
            def orange(cls): pass
        class PermissionOverwrite: pass
        class ChannelType:
            private_thread = 1
            public_thread = 2
        class Thread: pass
        class TextChannel: pass
        class CategoryChannel: pass
        class ForumChannel: pass
        class ApplicationContext: pass
    logger.info("discord service not installed; using dummy classes for type hinting.")

from .models import TicketToolConfig, ComplianceTicket, ComplianceTicketComment
from .app_settings import get_user_model

try:
    from aadiscordbot.cogs.utils.decorators import sender_is_admin
except ImportError:
    def sender_is_admin():
        def wrapper(func):
            return func
        return wrapper
    logger.info("aadiscordbot not installed; Discord commands will not be registered.")

try:
    from discord.commands import slash_command
    from discord.commands import SlashCommandGroup
    from discord.ext import commands
except ImportError:
    # Fallback for environments without discord.py (e.g. during migrations or when not using bot)
    class commands:
        class Cog:
            def __init__(self, *args, **kwargs): pass
            @classmethod
            def listener(cls, *args, **kwargs):
                def wrapper(func): return func
                return wrapper

    def slash_command(*args, **kwargs):
        def wrapper(func): return func
        return wrapper

    class SlashCommandGroup:
        def __init__(self, *args, **kwargs): pass
        def command(self, *args, **kwargs):
            def wrapper(func): return func
            return wrapper

    logger.info("discord service not installed; Discord commands will not work.")

def get_staff_roles():
    """Parse the comma-separated list of Discord role IDs allowed on tickets."""
    cfg = TicketToolConfig.get_solo()
    if not cfg.staff_roles:  # no staff roles configured → return empty list
        return []
    return [int(r.strip()) for r in cfg.staff_roles.split(",") if r.strip().isdigit()]

async def create_compliance_ticket(bot, user_id, discord_user_id: int, reason: str, message: str):
    category_id = TicketToolConfig.get_solo().Category_ID
    guild = bot.guilds[0]  # or use a known guild_id if multi-guild
    # Find or create a category with capacity (auto-clone with -2/-3 if needed)
    category = await ensure_ticket_category_with_capacity(guild, category_id)
    member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
    User = get_user_model()
    user = User.objects.get(id=user_id)
    profile = UserProfile.objects.get(user=user)

    staff_roles = get_staff_roles()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }

    for rid in staff_roles:
        role = guild.get_role(rid)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    ticket_number = get_next_ticket_number()

    channel = await guild.create_text_channel(
        name=f"ticket-{ticket_number}",
        category=category,
        overwrites=overwrites,
        topic=f"Compliance ticket for {profile.main_character} [{reason}]",
        reason="Compliance ticket creation",
    )

    # Use embeds and chunking for the initial message
    from .app_settings import _chunk_embed_lines
    lines = message.split("\n")
    chunks = _chunk_embed_lines(lines)

    for i, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=f"Compliance Ticket - {reason}" if i == 0 else None,
            description="\n".join(chunk),
            color=discord.Color.from_rgb(241, 196, 15)  # Gold
        )
        if i == 0:
            await channel.send(content=f"<@{discord_user_id}>", embed=embed)
        else:
            await channel.send(embed=embed)

    ComplianceTicket.objects.create(
        user=user,
        discord_user_id=member.id,
        discord_channel_id=channel.id,
        reason=reason,
        ticket_id=ticket_number,
    )


async def create_compliance_thread(bot, user_id, discord_user_id: int, reason: str, message: str, thread_name: str, thread_id: int = None):
    tcfg = TicketToolConfig.get_solo()
    parent_channel_id = tcfg.Forum_Channel_ID
    if not parent_channel_id:
        logger.error("Forum/Thread parent channel ID not configured")
        return

    guild = bot.guilds[0]
    parent_channel = bot.get_channel(parent_channel_id)
    if not parent_channel:
        try:
            parent_channel = await bot.fetch_channel(parent_channel_id)
        except Exception:
            logger.error(f"Could not find parent channel {parent_channel_id}")
            return

    User = get_user_model()
    user = User.objects.get(id=user_id)

    thread = None
    if thread_id:
        thread = bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await bot.fetch_channel(thread_id)
            except Exception:
                pass

        if thread and thread.archived:
            await thread.edit(archived=False)

    if not thread:
        # Create new thread
        if isinstance(parent_channel, discord.ForumChannel):
            # Forum threads are created with a starting message
            from .app_settings import _chunk_embed_lines
            lines = message.split("\n")
            chunks = _chunk_embed_lines(lines)

            # Initial message for the thread
            embed = discord.Embed(
                title=f"Compliance Ticket - {reason}",
                description="\n".join(chunks[0]),
                color=discord.Color.from_rgb(241, 196, 15)  # Gold
            )

            thread_with_msg = await parent_channel.create_thread(
                name=thread_name,
                content=f"<@{discord_user_id}>" if discord_user_id else None,
                embed=embed,
                reason="Compliance ticket creation"
            )
            thread = thread_with_msg.thread

            # Send remaining chunks if any
            if len(chunks) > 1:
                for chunk in chunks[1:]:
                    await thread.send(embed=discord.Embed(description="\n".join(chunk), color=discord.Color.from_rgb(241, 196, 15)))
        else:
            # TextChannel: Create private or public thread
            # User requested Option 1: Private threads in a channel
            thread_type = discord.ChannelType.private_thread if tcfg.ticket_type == TicketToolConfig.TICKET_TYPE_PRIVATE_THREAD else discord.ChannelType.public_thread

            thread = await parent_channel.create_thread(
                name=thread_name,
                type=thread_type,
                reason="Compliance ticket creation"
            )

            # Initial message
            from .app_settings import _chunk_embed_lines
            lines = message.split("\n")
            chunks = _chunk_embed_lines(lines)

            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"Compliance Ticket - {reason}" if i == 0 else None,
                    description="\n".join(chunk),
                    color=discord.Color.from_rgb(241, 196, 15)  # Gold
                )
                if i == 0:
                    await thread.send(content=f"<@{discord_user_id}>" if discord_user_id else None, embed=embed)
                else:
                    await thread.send(embed=embed)

        # Save thread mapping
        from .models import ComplianceThread
        ComplianceThread.objects.update_or_create(
            user=user, reason=reason,
            defaults={'thread_id': thread.id}
        )
        thread_id = thread.id

    # Ensure user is in the thread if it's a private thread
    if thread.type == discord.ChannelType.private_thread:
        try:
            member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
            if member:
                await thread.add_user(member)
        except Exception:
            pass

    # Create local ticket record
    ComplianceTicket.objects.create(
        user=user,
        discord_user_id=discord_user_id or 0,
        discord_channel_id=thread.id,
        reason=reason,
        ticket_id=get_next_ticket_number(),
    )


async def send_ticket_reminder(bot, channel_id: int, user_id: int, message: str):
    channel = bot.get_channel(channel_id)
    member = channel.guild.get_member(user_id)
    if channel and member:  # only send reminders when both channel and member resolve
        from .app_settings import _chunk_embed_lines
        lines = message.split("\n")
        chunks = _chunk_embed_lines(lines)

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title="Ticket Reminder" if i == 0 else None,
                description="\n".join(chunk),
                color=discord.Color.orange()
            )
            if i == 0:
                await channel.send(content=f"<@{user_id}>", embed=embed)
            else:
                await channel.send(embed=embed)

async def close_ticket_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            pass

    if channel:
        if ComplianceTicket.objects.filter(discord_channel_id=channel_id, is_resolved=False).exists():
            return

        if isinstance(channel, discord.Thread):
            await channel.edit(archived=True, locked=True, reason="Compliance issue resolved")
        else:
            await channel.delete(reason="Compliance issue resolved")

async def join_thread(bot, thread_id: int):
    channel = bot.get_channel(thread_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(thread_id)
        except Exception:
            pass

    if channel and isinstance(channel, discord.Thread):
        await channel.join()

async def unarchive_thread(bot, thread_id: int):
    channel = bot.get_channel(thread_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(thread_id)
        except Exception:
            pass

    if channel and isinstance(channel, discord.Thread):
        if channel.archived:
            await channel.edit(archived=False)
        await channel.join()

def get_next_ticket_number():
    """
    Returns the next ticket number as a zero-padded string (0000–9999),
    increments and wraps the counter in TicketToolConfig.
    """
    with transaction.atomic():
        cfg = TicketToolConfig.get_solo()
        num = cfg.ticket_counter or 0
        formatted = f"{num:04d}"  # zero-padded to 4 digits
        # increment & wrap
        cfg.ticket_counter = (num + 1) % 10000
        cfg.save(update_fields=["ticket_counter"])
    return formatted

class TicketCommands(commands.Cog):
    """Cog for operators handling compliance tickets via commands or phrases."""
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def ticket_message_listener(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Track Discord activity quietly
        await self._track_activity(message)

        # Check if this is a ticket channel/thread
        tickets = ComplianceTicket.objects.filter(
            discord_channel_id=message.channel.id,
            is_resolved=False
        )

        if not tickets.exists():
            return

        content = message.content.strip()
        if not content:
            return

        # Handle resolution command
        if content.lower() == "!resolved":
            await self._handle_resolution(message)
            return

        # Relay message as comment
        from allianceauth.services.modules.discord.models import DiscordUser
        auth_user = None
        try:
            du = DiscordUser.objects.select_related('user').get(uid=message.author.id)
            auth_user = du.user
        except DiscordUser.DoesNotExist:
            pass

        # If no auth user, prefix the comment with the Discord name
        relay_content = content
        if not auth_user:
            relay_content = f"[{message.author.display_name} on Discord]: {content}"

        for ticket in tickets:
            ComplianceTicketComment.objects.create(
                ticket=ticket,
                user=auth_user,
                comment=relay_content
            )

    async def _track_activity(self, message: discord.Message):
        """Update last_discord_message_at for the author, throttled to 1h."""
        uid = message.author.id
        cache_key = f"aa_bb_discord_activity_{uid}"

        if not cache.get(cache_key):
            from allianceauth.services.modules.discord.models import DiscordUser
            from .models import UserStatus
            try:
                du = DiscordUser.objects.select_related('user').get(uid=uid)
                UserStatus.objects.update_or_create(
                    user=du.user,
                    defaults={'last_discord_message_at': timezone.now()}
                )
                # Cache for 1 hour to avoid excessive DB writes
                cache.set(cache_key, True, 3600)
            except DiscordUser.DoesNotExist:
                # Not a linked user, ignore
                pass
            except Exception:
                logger.exception("Failed to track Discord activity for UID %s", uid)

    @slash_command(
        name="resolve-ticket",
        description="Mark this ticket as resolved and close/lock the channel."
    )
    @sender_is_admin()
    async def resolve_ticket_slash(self, ctx: discord.ApplicationContext):
        await self._handle_resolution(ctx)

    async def _handle_resolution(self, ctx_or_msg):
        channel = ctx_or_msg.channel
        author = ctx_or_msg.author if isinstance(ctx_or_msg, discord.Message) else ctx_or_msg.user

        # Permission check: must be admin or have staff role
        staff_roles = get_staff_roles()
        is_staff = author.guild_permissions.administrator or any(role.id in staff_roles for role in author.roles)

        # Check AA permissions if DiscordUser is linked
        if not is_staff:
            try:
                from allianceauth.services.modules.discord.models import DiscordUser
                discord_user = DiscordUser.objects.select_related('user').get(uid=author.id)
                if discord_user.user.has_perm("aa_bb.ticket_manager") or discord_user.user.is_superuser:
                    is_staff = True
            except Exception:
                pass

        if not is_staff:
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond("You do not have permission to resolve tickets.", ephemeral=True)
            return

        tickets = ComplianceTicket.objects.filter(
            discord_channel_id=channel.id,
            is_resolved=False,
        )

        if not tickets.exists():
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond("No open ticket found for this channel.", ephemeral=True)
            return

        # Resolve all tickets in this channel
        for ticket in tickets:
            if ticket.reason in ["char_removed", "awox_kill"]:
                ticket.is_resolved = True
                ticket.save(update_fields=["is_resolved"])
            else:
                ticket.delete()

        # If no more active tickets in this channel, close it
        if not ComplianceTicket.objects.filter(discord_channel_id=channel.id, is_resolved=False).exists():
            msg = f"✅ All issues resolved by <@{author.id}>. Closing channel..."
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond(msg)
            else:
                await channel.send(msg)

            if isinstance(channel, discord.Thread):
                await channel.edit(archived=True, locked=True)
            else:
                await channel.delete(reason=f"Resolved by {author}")
        else:
            msg = f"✅ Ticket(s) resolved by <@{author.id}>. (Remaining active tickets exist in this channel)"
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond(msg)
            else:
                await channel.send(msg)

    @slash_command(
        name="resolve-char-removed",
        description="Mark this channel's 'char_removed' ticket as resolved (no channel/DB deletion)."
    )
    @sender_is_admin()
    async def resolve_char_removed(self, ctx: discord.ApplicationContext):
        await self.resolve_ticket_slash(ctx)

def setup(bot):
    bot.add_cog(TicketCommands(bot))

# ---- Category overflow helpers ----

CATEGORY_LIMIT = 50  # Discord hard limit per category

def _parse_family_suffix(base_name: str, candidate_name: str) -> int | None:
    """
    Return the numeric suffix for a candidate category in the same family as base_name.
    Base category => 1, clones => 2, 3, ...; None if not in family.
    """
    if candidate_name == base_name:  # exact match → treat as suffix 1
        return 1
    # Match exact base name followed by dash and a positive integer
    m = re.fullmatch(rf"{re.escape(base_name)}-(\d+)", candidate_name)
    if not m:
        return None
    try:
        n = int(m.group(1))
        if n >= 2:  # only treat "-2"/"-3"/... as valid
            return n
    except Exception:
        pass
    return None

def _get_family_categories(guild: discord.Guild, base_category: discord.CategoryChannel) -> list[tuple[int, discord.CategoryChannel]]:
    """
    Discover all categories that belong to the ticket family: base name and "-N" clones.
    Returns a sorted list of (suffix_number, category) with base as 1.
    """
    fam: list[tuple[int, discord.CategoryChannel]] = []
    base_name = base_category.name
    for cat in guild.categories:
        suf = _parse_family_suffix(base_name, cat.name)
        if suf is not None:  # only include categories that follow the naming convention
            fam.append((suf, cat))
    fam.sort(key=lambda x: x[0])
    return fam

async def ensure_ticket_category_with_capacity(guild: discord.Guild, base_category_id: int) -> discord.CategoryChannel:
    """
    Ensure there is a category in the ticket family with available capacity.
    - Try base, then -2, -3 in order.
    - If all are full, create next clone suffixed category and return it.
    """
    base = guild.get_channel(base_category_id)
    if not isinstance(base, discord.CategoryChannel):
        raise RuntimeError("Configured Category_ID is not a valid category")

    family = _get_family_categories(guild, base)
    for _, cat in family:
        try:
            if len(cat.channels) < CATEGORY_LIMIT:
                return cat
        except Exception:  # defensive guard in case Discord returns odd data
            continue

    # All full: create next clone
    next_suffix = (family[-1][0] + 1) if family else 2  # base missing → start at -2
    name = f"{base.name}-{next_suffix}"
    # Copy overwrites from base
    overwrites = base.overwrites
    new_cat = await guild.create_category(
        name=name,
        overwrites=overwrites,
        reason="Auto-created ticket overflow category",
        position=base.position + next_suffix - 1 if hasattr(base, "position") else None,
    )
    return new_cat

def _is_ticket_channel(ch: discord.abc.GuildChannel) -> bool:
    return (
        isinstance(ch, discord.TextChannel)
        and (
            (ch.name or "").startswith("ticket-")
            or (getattr(ch, "topic", None) or "").lower().startswith("compliance ticket")
        )
    )

async def rebalance_ticket_categories(bot):
    """
    Try to keep earlier categories in the ticket family as full as possible by moving
    ticket channels leftwards. Delete empty overflow categories (suffix >= 2).
    """
    cfg = TicketToolConfig.get_solo()
    if not cfg.Category_ID:  # nothing configured → nothing to rebalance
        return
    if not bot.guilds:  # ensure the bot is connected to at least one guild
        return
    guild = bot.guilds[0]
    base = guild.get_channel(int(cfg.Category_ID))
    if not isinstance(base, discord.CategoryChannel):  # invalid configuration
        return

    family = _get_family_categories(guild, base)
    if not family:
        return

    MOVE_LIMIT = 30
    moves = 0

    # Build lists of ticket channels per category (only tickets)
    cats = [cat for _, cat in family]
    tickets_by_cat: dict[int, list[discord.TextChannel]] = {
        cat.id: [ch for ch in cat.channels if _is_ticket_channel(ch)] for cat in cats
    }

    # Fill earlier categories from later ones
    for idx in range(1, len(cats)):
        if moves >= MOVE_LIMIT:  # avoid shuffling too many channels per invocation
            break
        left = cats[idx - 1]
        right = cats[idx]

        def left_capacity() -> int:
            try:
                return CATEGORY_LIMIT - len(left.channels)
            except Exception:
                return 0

        while left_capacity() > 0 and tickets_by_cat.get(right.id) and moves < MOVE_LIMIT:
            ch = tickets_by_cat[right.id].pop(0)
            try:
                await ch.edit(category=left, reason="Ticket overflow rebalancing")
                moves += 1
                # Track it in left collection if needed for subsequent steps
                tickets_by_cat.setdefault(left.id, []).append(ch)
            except discord.HTTPException:  # skip problematic/missing channel gracefully
                continue

    # Delete empty overflow categories (suffix >= 2)
    for suffix, cat in reversed(family):
        if suffix >= 2 and len(cat.channels) == 0:  # remove empty overflow categories
            try:
                await cat.delete(reason="Removing empty ticket overflow category")
            except discord.HTTPException:
                pass
