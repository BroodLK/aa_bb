"""
Discord ticket helper utilities used by BigBrother.

The functions here are called from Celery tasks as well as slash commands to
create/rebalance compliance ticket channels.
"""

from __future__ import annotations

import re

from allianceauth.authentication.models import UserProfile
from allianceauth.services.hooks import get_extension_logger

from django.db import transaction, close_old_connections
from django.core.cache import cache
from django.utils import timezone
from asgiref.sync import sync_to_async

logger = get_extension_logger(__name__)
logger.info("✅ [AA-BB] - [Tasks Bot] - Module loading from %s", __file__)

__all__ = [
    "create_compliance_ticket",
    "create_compliance_thread",
    "send_ticket_reminder",
    "close_ticket_channel",
    "join_thread",
    "unarchive_thread",
    "rebalance_ticket_categories",
    "TicketCommands",
    "setup",
]

try:
    import discord
    try:
        import discord.abc
    except ImportError:
        pass
    try:
        import discord.utils
    except ImportError:
        pass
except ImportError:
    class discord:
        class Message: pass
        class Embed:
            def __init__(self, *args, **kwargs):
                for k, v in kwargs.items():
                    setattr(self, k, v)
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
        class Guild: pass
        class abc:
            class GuildChannel: pass
        class utils:
            @staticmethod
            def get(*args, **kwargs): pass
        class HTTPException(Exception): pass
    logger.info("discord service not installed; using dummy classes for type hinting.")

from aa_bb.models import TicketToolConfig, ComplianceTicket, ComplianceTicketComment
from aa_bb.app_settings import get_user_model

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

def get_ticket_roles():
    """Parse the comma-separated list of Discord role IDs from TicketToolConfig."""
    cfg = TicketToolConfig.get_solo()
    roles = []
    if cfg.role_id:
        for r in cfg.role_id.split(","):
            r = r.strip()
            if not r:
                continue
            if r.isdigit():
                roles.append(int(r))
            else:
                roles.append(r)
    return roles

async def create_compliance_ticket(bot, user_id, discord_user_id: int, reason: str, message: str, include_user: bool = True, **kwargs):
    close_old_connections()
    tcfg = await sync_to_async(TicketToolConfig.get_solo)()
    category_id = tcfg.Category_ID
    if not category_id:
        logger.error("Compliance ticket category ID not configured")
        return

    base_category = bot.get_channel(category_id)
    if not base_category:
        try:
            base_category = await bot.fetch_channel(category_id)
        except Exception:
            logger.error(f"Could not find category {category_id}")
            return
    guild = base_category.guild

    # Find or create a category with capacity (auto-clone with -2/-3 if needed)
    category = await ensure_ticket_category_with_capacity(guild, category_id)

    member = None
    if discord_user_id:
        try:
            member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
        except Exception:
            logger.warning(f"Could not find member {discord_user_id} in guild {guild.id}")
    User = get_user_model()
    user = await sync_to_async(User.objects.get)(id=user_id)
    profile = await sync_to_async(UserProfile.objects.get)(user=user)

    staff_roles = await sync_to_async(get_ticket_roles)()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }

    if include_user and member:
        overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    for r_val in staff_roles:
        role = None
        if isinstance(r_val, int):
            role = guild.get_role(r_val)
            if not role:
                try:
                    role = await guild.fetch_role(r_val)
                except Exception:
                    continue
        else:
            role = discord.utils.get(guild.roles, name=r_val)

        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    ticket_number = await sync_to_async(get_next_ticket_number)()

    channel = await guild.create_text_channel(
        name=f"ticket-{ticket_number}",
        category=category,
        overwrites=overwrites,
        topic=f"Compliance ticket for {profile.main_character} [{reason}]",
        reason="Compliance ticket creation",
    )

    # Use embeds and chunking for the initial message
    from aa_bb.app_settings import _chunk_embed_lines
    lines = message.split("\n")
    chunks = _chunk_embed_lines(lines)

    pings = []
    if include_user and discord_user_id:
        pings.append(f"<@{discord_user_id}>")

    ticket_roles = await sync_to_async(get_ticket_roles)()
    for r_val in ticket_roles:
        if isinstance(r_val, int):
            pings.append(f"<@&{r_val}>")

    content = " ".join(pings) if pings else None

    for i, chunk in enumerate(chunks):
        embed = discord.Embed(
            title=f"Compliance Ticket - {reason}" if i == 0 else None,
            description="\n".join(chunk),
            color=discord.Color.from_rgb(241, 196, 15)  # Gold
        )
        if i == 0:
            await channel.send(content=content, embed=embed)
        else:
            await channel.send(embed=embed)

    await sync_to_async(ComplianceTicket.objects.create)(
        user=user,
        discord_user_id=discord_user_id or 0,
        discord_channel_id=channel.id,
        reason=reason,
        ticket_id=ticket_number,
    )


async def create_compliance_thread(bot, user_id, discord_user_id: int, reason: str, message: str, thread_name: str, thread_id: int = None, include_user: bool = True, **kwargs):
    close_old_connections()
    tcfg = await sync_to_async(TicketToolConfig.get_solo)()
    parent_channel_id = tcfg.Forum_Channel_ID
    if not parent_channel_id:
        logger.error("Forum/Thread parent channel ID not configured")
        return

    parent_channel = bot.get_channel(parent_channel_id)
    if not parent_channel:
        try:
            parent_channel = await bot.fetch_channel(parent_channel_id)
        except Exception:
            logger.error(f"Could not find parent channel {parent_channel_id}")
            return
    guild = parent_channel.guild

    # Truncate thread name to Discord limit of 100
    if len(thread_name) > 100:
        thread_name = thread_name[:97] + "..."

    User = get_user_model()
    user = await sync_to_async(User.objects.get)(id=user_id)

    thread = None
    if thread_id:
        thread = bot.get_channel(thread_id)
        if not thread:
            try:
                thread = await bot.fetch_channel(thread_id)
            except Exception:
                pass

        if thread and thread.archived:
            try:
                await thread.edit(archived=False)
            except Exception:
                logger.exception(f"Failed to unarchive thread {thread_id}")

    if not thread:
        # Create new thread
        pings = []
        if include_user and discord_user_id:
            pings.append(f"<@{discord_user_id}>")

        ticket_roles = await sync_to_async(get_ticket_roles)()
        for r_val in ticket_roles:
            if isinstance(r_val, int):
                pings.append(f"<@&{r_val}>")

        content = " ".join(pings) if pings else None

        if isinstance(parent_channel, discord.ForumChannel):
            # Forum threads are created with a starting message
            from aa_bb.app_settings import _chunk_embed_lines
            lines = message.split("\n")
            chunks = _chunk_embed_lines(lines)

            # Initial message for the thread
            embed = discord.Embed(
                title=f"Compliance Ticket - {reason}",
                description="\n".join(chunks[0]),
                color=discord.Color.from_rgb(241, 196, 15)  # Gold
            )

            thread_response = await parent_channel.create_thread(
                name=thread_name,
                content=content,
                embed=embed,
                reason="Compliance ticket creation"
            )
            thread = getattr(thread_response, 'thread', thread_response)

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
            from aa_bb.app_settings import _chunk_embed_lines
            lines = message.split("\n")
            chunks = _chunk_embed_lines(lines)

            for i, chunk in enumerate(chunks):
                embed = discord.Embed(
                    title=f"Compliance Ticket - {reason}" if i == 0 else None,
                    description="\n".join(chunk),
                    color=discord.Color.from_rgb(241, 196, 15)  # Gold
                )
                if i == 0:
                    await thread.send(content=content, embed=embed)
                else:
                    await thread.send(embed=embed)

        # Save thread mapping
        from aa_bb.models import ComplianceThread
        await sync_to_async(ComplianceThread.objects.update_or_create)(
            user=user, reason=reason,
            defaults={'thread_id': thread.id}
        )
        thread_id = thread.id

    # Ensure user and staff are in the thread
    # User
    if include_user and discord_user_id:
        try:
            # Using fetch_member to ensure we get the member object even if not in cache
            target_member = guild.get_member(discord_user_id) or await guild.fetch_member(discord_user_id)
            if target_member:
                await thread.add_user(target_member)
                logger.info(f"Added user {target_member} to thread {thread.id}")
        except Exception:
            logger.warning(f"Failed to add user {discord_user_id} to thread {thread.id}")

    # Staff
    staff_roles = await sync_to_async(get_ticket_roles)()
    if staff_roles:
        # Try to ensure members are cached for staff roles
        try:
            first_role_val = staff_roles[0]
            first_role = None
            if isinstance(first_role_val, int):
                first_role = guild.get_role(first_role_val)
            else:
                first_role = discord.utils.get(guild.roles, name=first_role_val)

            if first_role and not first_role.members and guild.member_count < 3000:
                logger.info(f"Staff role cache empty, fetching members for guild {guild.id}")
                await guild.fetch_members(limit=None)
        except Exception:
            pass

        for r_val in staff_roles:
            role = None
            if isinstance(r_val, int):
                role = guild.get_role(r_val)
                if not role:
                    try: role = await guild.fetch_role(r_val)
                    except: pass
            else:
                role = discord.utils.get(guild.roles, name=r_val)

            if role:
                # Limit thread additions to avoid hitting rate limits or Discord thread member limits
                members_to_add = list(role.members)
                if len(members_to_add) > 50:
                    logger.warning(f"Role {role.name} has too many members ({len(members_to_add)}), only adding first 50 to thread {thread.id}")
                    members_to_add = members_to_add[:50]

                for m in members_to_add:
                    try:
                        await thread.add_user(m)
                    except Exception:
                        pass

    # Create local ticket record
    await sync_to_async(ComplianceTicket.objects.create)(
        user=user,
        discord_user_id=discord_user_id or 0,
        discord_channel_id=thread.id,
        reason=reason,
        ticket_id=await sync_to_async(get_next_ticket_number)(),
    )


async def send_ticket_reminder(bot, channel_id: int, user_id: int, message: str, **kwargs):
    channel = bot.get_channel(channel_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            pass

    if channel:
        from aa_bb.app_settings import _chunk_embed_lines
        lines = message.split("\n")
        chunks = _chunk_embed_lines(lines)

        content = f"<@{user_id}>" if user_id else None

        for i, chunk in enumerate(chunks):
            embed = discord.Embed(
                title="Ticket Comment" if i == 0 else None,
                description="\n".join(chunk),
                color=discord.Color.orange()
            )
            if i == 0:
                await channel.send(content=content, embed=embed)
            else:
                await channel.send(embed=embed)

async def close_ticket_channel(bot, channel_id: int, message: str = None, **kwargs):
    close_old_connections()
    channel = bot.get_channel(channel_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            pass

    if channel:
        if message:
            embed = discord.Embed(
                title="Ticket Comment",
                description=message,
                color=discord.Color.orange()
            )
            await channel.send(embed=embed)
            import asyncio
            await asyncio.sleep(1)

        qs = ComplianceTicket.objects.filter(discord_channel_id=channel_id, is_resolved=False)
        if await sync_to_async(qs.exists)():
            return

        try:
            if isinstance(channel, discord.Thread):
                await channel.edit(archived=True, locked=True, reason="Compliance issue resolved")
            else:
                await channel.delete(reason="Compliance issue resolved")
        except Exception:
            logger.exception("Failed to close/delete channel %s", channel_id)

async def join_thread(bot, thread_id: int, **kwargs):
    channel = bot.get_channel(thread_id)
    if not channel and hasattr(bot, 'fetch_channel'):
        try:
            channel = await bot.fetch_channel(thread_id)
        except Exception:
            pass

    if channel and isinstance(channel, discord.Thread):
        await channel.join()

async def unarchive_thread(bot, thread_id: int, **kwargs):
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

        close_old_connections()

        # Track Discord activity quietly
        await self._track_activity(message)

        # Check if this is a ticket channel/thread
        tickets_qs = ComplianceTicket.objects.filter(
            discord_channel_id=message.channel.id,
            is_resolved=False
        )
        tickets = await sync_to_async(list)(tickets_qs)

        if not tickets:
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
            du = await sync_to_async(DiscordUser.objects.select_related('user').get)(uid=message.author.id)
            auth_user = du.user
        except DiscordUser.DoesNotExist:
            pass

        # If no auth user, prefix the comment with the Discord name
        relay_content = content
        if not auth_user:
            relay_content = f"[{message.author.display_name} on Discord]: {content}"

        for ticket in tickets:
            await sync_to_async(ComplianceTicketComment.objects.create)(
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
            from aa_bb.models import UserStatus
            try:
                du = await sync_to_async(DiscordUser.objects.select_related('user').get)(uid=uid)
                await sync_to_async(UserStatus.objects.update_or_create)(
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
        name="resolve-compliance-ticket",
        description="Mark this ticket as resolved and close/lock the channel."
    )
    @sender_is_admin()
    async def resolve_ticket_slash(self, ctx: discord.ApplicationContext):
        await ctx.defer(ephemeral=True)
        await self._handle_resolution(ctx)

    async def _handle_resolution(self, ctx_or_msg):
        close_old_connections()
        channel = ctx_or_msg.channel
        author = ctx_or_msg.author if isinstance(ctx_or_msg, discord.Message) else ctx_or_msg.user

        # Permission check: must be admin or have staff role
        staff_roles = await sync_to_async(get_ticket_roles)()
        is_staff = author.guild_permissions.administrator or any(role.id in staff_roles for role in author.roles)

        # Check AA permissions and resolve auth_user
        from allianceauth.services.modules.discord.models import DiscordUser
        auth_user = None
        try:
            du_qs = DiscordUser.objects.select_related('user').filter(uid=author.id)
            discord_user = await sync_to_async(du_qs.get)()
            auth_user = discord_user.user

            def check_perms(user):
                return user.has_perm("aa_bb.ticket_manager") or user.is_superuser

            if not is_staff and await sync_to_async(check_perms)(auth_user):
                is_staff = True
        except Exception:
            pass

        if not is_staff:
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond("You do not have permission to resolve tickets.", ephemeral=True)
            return

        tickets_qs = ComplianceTicket.objects.filter(
            discord_channel_id=channel.id,
            is_resolved=False,
        )
        tickets = await sync_to_async(list)(tickets_qs)

        if not tickets:
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond("No open ticket found for this channel.", ephemeral=True)
            return

        # Resolve all tickets in this channel
        for ticket in tickets:
            ticket.is_resolved = True
            await sync_to_async(ticket.save)(update_fields=["is_resolved"])

            # Create Auth comment
            comment_text = f"✅ Ticket resolved on Discord by {author}."
            await sync_to_async(ComplianceTicketComment.objects.create)(
                ticket=ticket,
                user=auth_user,
                comment=comment_text
            )

        # If no more active tickets in this channel, close it
        remaining_qs = ComplianceTicket.objects.filter(discord_channel_id=channel.id, is_resolved=False)
        if not await sync_to_async(remaining_qs.exists)():
            msg = f"✅ All issues resolved by <@{author.id}>. Closing channel..."
            embed = discord.Embed(
                title="Ticket Comment",
                description=msg,
                color=discord.Color.orange()
            )
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond(embed=embed)
            else:
                await channel.send(embed=embed)

            try:
                if isinstance(channel, discord.Thread):
                    await channel.edit(archived=True, locked=True)
                else:
                    await channel.delete(reason=f"Resolved by {author}")
            except Exception:
                logger.exception("Failed to close/delete channel %s after resolution", channel.id)
        else:
            msg = f"✅ Ticket(s) resolved by <@{author.id}>. (Remaining active tickets exist in this channel)"
            embed = discord.Embed(
                title="Ticket Comment",
                description=msg,
                color=discord.Color.orange()
            )
            if hasattr(ctx_or_msg, "respond"):
                await ctx_or_msg.respond(embed=embed)
            else:
                await channel.send(embed=embed)

    @slash_command(
        name="mark-ticket-as-exception",
        description="Mark this ticket as an exception (won't receive reminders or be recreated)."
    )
    @sender_is_admin()
    async def mark_ticket_as_exception(
        self,
        ctx: discord.ApplicationContext,
        reason: str = None
    ):
        """Mark a ticket as an exception with an optional reason."""
        await ctx.defer(ephemeral=True)
        close_old_connections()
        channel = ctx.channel
        author = ctx.user

        # Permission check: must be admin or have staff role
        staff_roles = await sync_to_async(get_ticket_roles)()
        is_staff = author.guild_permissions.administrator or any(role.id in staff_roles for role in author.roles)

        # Check AA permissions and resolve auth_user
        from allianceauth.services.modules.discord.models import DiscordUser
        auth_user = None
        try:
            du_qs = DiscordUser.objects.select_related('user').filter(uid=author.id)
            discord_user = await sync_to_async(du_qs.get)()
            auth_user = discord_user.user

            def check_perms(user):
                return user.has_perm("aa_bb.ticket_manager") or user.is_superuser

            if not is_staff and await sync_to_async(check_perms)(auth_user):
                is_staff = True
        except Exception:
            pass

        if not is_staff:
            await ctx.respond("You do not have permission to mark tickets as exceptions.", ephemeral=True)
            return

        tickets_qs = ComplianceTicket.objects.filter(
            discord_channel_id=channel.id,
            is_resolved=False,
        )
        tickets = await sync_to_async(list)(tickets_qs)

        if not tickets:
            await ctx.respond("No open ticket found for this channel.", ephemeral=True)
            return

        # Mark all tickets in this channel as exceptions
        for ticket in tickets:
            ticket.is_exception = True
            ticket.exception_reason = reason or f"Marked as exception by {author.display_name}"
            await sync_to_async(ticket.save)(update_fields=["is_exception", "exception_reason"])

            # Create Auth comment
            comment_text = f"ℹ️ Ticket marked as exception on Discord by {author}."
            if reason:
                comment_text += f"\nReason: {reason}"
            await sync_to_async(ComplianceTicketComment.objects.create)(
                ticket=ticket,
                user=auth_user,
                comment=comment_text
            )

        exception_msg = f"✅ Ticket(s) marked as exception by <@{author.id}>."
        if reason:
            exception_msg += f"\nReason: {reason}"

        embed1 = discord.Embed(
            title="Ticket Comment",
            description=exception_msg,
            color=discord.Color.orange()
        )
        await ctx.respond(embed=embed1)

        embed2 = discord.Embed(
            title="Ticket Comment",
            description=f"ℹ️ This ticket has been marked as an exception and will not receive reminders or be recreated.",
            color=discord.Color.orange()
        )
        await channel.send(embed=embed2)

    @commands.Cog.listener("on_guild_channel_delete")
    async def on_channel_delete(self, channel: discord.abc.GuildChannel):
        """Mark tickets as resolved when a channel is manually deleted."""
        close_old_connections()
        tickets_qs = ComplianceTicket.objects.filter(
            discord_channel_id=channel.id,
            is_resolved=False
        )
        tickets = await sync_to_async(list)(tickets_qs)

        for ticket in tickets:
            ticket.is_resolved = True
            await sync_to_async(ticket.save)(update_fields=["is_resolved"])
            await sync_to_async(ComplianceTicketComment.objects.create)(
                ticket=ticket,
                comment="✅ Ticket automatically resolved (Discord channel deleted manually)."
            )

        if tickets:
            logger.info(f"Marked {len(tickets)} ticket(s) as resolved due to channel deletion: {channel.id}")

    @commands.Cog.listener("on_thread_delete")
    async def on_thread_delete(self, thread: discord.Thread):
        """Mark tickets as resolved when a thread is manually deleted."""
        close_old_connections()
        tickets_qs = ComplianceTicket.objects.filter(
            discord_channel_id=thread.id,
            is_resolved=False
        )
        tickets = await sync_to_async(list)(tickets_qs)

        for ticket in tickets:
            ticket.is_resolved = True
            await sync_to_async(ticket.save)(update_fields=["is_resolved"])
            await sync_to_async(ComplianceTicketComment.objects.create)(
                ticket=ticket,
                comment="✅ Ticket automatically resolved (Discord thread deleted manually)."
            )

        if tickets:
            logger.info(f"Marked {len(tickets)} ticket(s) as resolved due to thread deletion: {thread.id}")

    @commands.Cog.listener("on_thread_update")
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        """Handle manual thread archiving/locking by an admin."""
        if (after.archived and not before.archived) or (after.locked and not before.locked):
            close_old_connections()
            tickets_qs = ComplianceTicket.objects.filter(
                discord_channel_id=after.id,
                is_resolved=False
            )
            tickets = await sync_to_async(list)(tickets_qs)
            if tickets:
                for ticket in tickets:
                    ticket.is_resolved = True
                    await sync_to_async(ticket.save)(update_fields=["is_resolved"])
                    await sync_to_async(ComplianceTicketComment.objects.create)(
                        ticket=ticket,
                        comment=f"✅ Ticket automatically resolved (Discord thread {'archived' if after.archived else 'locked'} manually)."
                    )
                logger.info(f"Marked {len(tickets)} ticket(s) as resolved due to manual thread closure: {after.id}")

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
    if not base:
        try:
            base = await guild.fetch_channel(base_category_id)
        except Exception:
            pass

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

async def rebalance_ticket_categories(bot, **kwargs):
    """
    Try to keep earlier categories in the ticket family as full as possible by moving
    ticket channels leftwards. Delete empty overflow categories (suffix >= 2).
    """
    close_old_connections()
    cfg = await sync_to_async(TicketToolConfig.get_solo)()
    if not cfg.Category_ID:  # nothing configured → nothing to rebalance
        return
    if not bot.guilds:  # ensure the bot is connected to at least one guild
        return
    guild = bot.guilds[0]
    base = guild.get_channel(int(cfg.Category_ID))
    if not base:
        try:
            base = await guild.fetch_channel(int(cfg.Category_ID))
        except Exception:
            pass

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
