"""
Discord ticket helper utilities used by BigBrother.

The functions here are called from Celery tasks as well as slash commands to
create/rebalance compliance ticket channels.
"""

import re
import logging

from allianceauth.authentication.models import UserProfile

from django.db import transaction

logger = logging.getLogger(__name__)

try:
    import discord
except ImportError:
    logger.error("discord service not installed; compliance checks will not work.")

from .models import TicketToolConfig, ComplianceTicket
from .app_settings import get_user_model

try:
    from aadiscordbot.cogs.utils.decorators import sender_is_admin
except ImportError:
    logger.error("aadiscordbot not installed; compliance checks will not work.")

try:
    from discord.commands import slash_command
    from discord.commands import SlashCommandGroup
    from discord.ext import commands
except ImportError:
    logger.error("discord service not installed; compliance checks will not work.")

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

    await channel.send(message)

    ComplianceTicket.objects.create(
        user=user,
        discord_user_id=member.id,
        discord_channel_id=channel.id,
        reason=reason,
        ticket_id=ticket_number,
    )


async def send_ticket_reminder(bot, channel_id: int, user_id: int, message: str):
    channel = bot.get_channel(channel_id)
    member = channel.guild.get_member(user_id)
    if channel and member:  # only send reminders when both channel and member resolve
        await channel.send(message)

async def close_ticket_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel:
        await channel.delete(reason="Compliance issue resolved")

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

class CharRemovedCommands(commands.Cog):
    """Slash-command cog for operators handling character removal tickets."""
    def __init__(self, bot):
        self.bot = bot

    @slash_command(
        name="resolve-char-removed",
        description="Mark this channel's 'char_removed' ticket as resolved (no channel/DB deletion)."
    )
    @sender_is_admin()
    async def resolve_char_removed(self, ctx: discord.ApplicationContext):
        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):  # ensure the command is run inside a ticket channel
            await ctx.respond("Use this in a ticket text channel.", ephemeral=True)
            return

        ticket = ComplianceTicket.objects.filter(
            discord_channel_id=channel.id,
            is_resolved=False,
        ).first()

        if not ticket:  # no matching ticket entry for this channel
            await ctx.respond("No open ticket found for this channel.", ephemeral=True)
            return

        if ticket.reason != "char_removed" and ticket.reason != "awox_kill":  # limit to supported ticket reasons
            await ctx.respond("This command only works for 'char_removed' and 'awox_kill' tickets.", ephemeral=True)
            return

        ticket.is_resolved = True
        ticket.save(update_fields=["is_resolved"])

        await ctx.respond(
            f"✅ Ticket for <@{ticket.discord_user_id}> marked resolved by <@{ctx.author.id}>.",
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(CharRemovedCommands(bot))

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
