"""
Utility commands: interactive /help, ping, bot/server/user info, avatars,
restart-safe reminders, and invite statistics.
"""

import platform
import time
from typing import TYPE_CHECKING, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import (
    COLOR_INFO,
    Paginator,
    error_embed,
    format_duration,
    info_embed,
    ok_embed,
    parse_duration,
)

if TYPE_CHECKING:
    from core import BurgBot

MAX_REMINDER_SECONDS = 90 * 24 * 3600

# cog name -> (emoji, blurb) for /help. Cogs not listed here are hidden.
CATEGORY_META = {
    "Referrals": ("📈", "Invite tracking, rewards, leaderboard, anti-abuse review"),
    "Tickets": ("🎫", "Ticket-opener detection and scheduled role removals"),
    "Welcome": ("👋", "Welcome and goodbye messages"),
    "Moderation": ("🛡️", "Purge, kick, ban, timeouts, slowmode, warnings"),
    "Utility": ("🧰", "Info commands, reminders, invites"),
    "Fun": ("🎲", "Polls, dice, coin flips, and other party tricks"),
    "Admin": ("⚙️", "Server setup wizard and bot configuration"),
}


def _category_embed(bot: "BurgBot", cog_name: str) -> discord.Embed:
    emoji, blurb = CATEGORY_META[cog_name]
    cog = bot.get_cog(cog_name)
    embed = discord.Embed(title=f"{emoji} {cog_name}", description=blurb, color=COLOR_INFO)
    for cmd in sorted(cog.get_commands(), key=lambda c: c.name):
        if cmd.hidden:
            continue
        signature = f" {cmd.signature}" if cmd.signature else ""
        embed.add_field(
            name=f"/{cmd.name}{signature}",
            value=cmd.description or cmd.short_doc or "*no description*",
            inline=False,
        )
    embed.set_footer(text="Every command also works with the classic prefix, e.g. !" + "help")
    return embed


def _overview_embed(bot: "BurgBot") -> discord.Embed:
    embed = discord.Embed(
        title="🍔 BurgBot — help",
        description=(
            "Pick a category from the dropdown below.\n\n"
            "Every command works two ways: as a **slash command** (`/referrals`) with Discord's "
            "autocomplete, or as a **prefix command** (`!referrals`)."
        ),
        color=COLOR_INFO,
    )
    for cog_name, (emoji, blurb) in CATEGORY_META.items():
        cog = bot.get_cog(cog_name)
        if cog is None:
            continue
        visible = [c for c in cog.get_commands() if not c.hidden]
        embed.add_field(name=f"{emoji} {cog_name} ({len(visible)})", value=blurb, inline=False)
    return embed


class HelpView(discord.ui.View):
    def __init__(self, bot: "BurgBot", author_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.category_select.options = [
            discord.SelectOption(label="Overview", value="__overview__", emoji="🏠")
        ] + [
            discord.SelectOption(label=name, value=name, emoji=emoji, description=blurb[:100])
            for name, (emoji, blurb) in CATEGORY_META.items()
            if bot.get_cog(name) is not None
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Run `/help` yourself to browse.", ephemeral=True)
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    @discord.ui.select(placeholder="Choose a command category…")
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        embed = _overview_embed(self.bot) if choice == "__overview__" else _category_embed(self.bot, choice)
        await interaction.response.edit_message(embed=embed, view=self)


class Utility(commands.Cog):
    """Info commands, reminders, and other everyday tools."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="help", description="Browse everything the bot can do, by category.")
    async def help(self, ctx: commands.Context):
        """Browse everything the bot can do, by category."""
        view = HelpView(self.bot, ctx.author.id)
        view.message = await ctx.send(embed=_overview_embed(self.bot), view=view)

    @commands.hybrid_command(name="ping", description="Check the bot's latency.")
    async def ping(self, ctx: commands.Context):
        """Check the bot's latency."""
        started = time.perf_counter()
        message = await ctx.send(embed=info_embed("Pinging… 🏓"))
        round_trip = (time.perf_counter() - started) * 1000
        embed = info_embed(
            f"🏓 **Pong!**\nWebSocket: **{self.bot.latency * 1000:.0f}ms**\nRound trip: **{round_trip:.0f}ms**"
        )
        # Slash invocations return an interaction message; edit works for both.
        try:
            await message.edit(embed=embed)
        except (discord.HTTPException, discord.Forbidden):
            pass

    @commands.hybrid_command(name="botinfo", description="Uptime, stats, and version info for the bot.")
    async def botinfo(self, ctx: commands.Context):
        """Uptime, stats, and version info for the bot."""
        uptime_seconds = int((discord.utils.utcnow() - self.bot.launch_time).total_seconds())
        total_members = sum(g.member_count or 0 for g in self.bot.guilds)
        command_count = len([c for c in self.bot.commands if not c.hidden])
        embed = info_embed("", title="🍔 BurgBot")
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Uptime", value=format_duration(uptime_seconds) or "just started")
        embed.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f}ms")
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Members served", value=f"{total_members:,}")
        embed.add_field(name="Commands", value=str(command_count))
        embed.add_field(name="Running on", value=f"discord.py {discord.__version__} · Python {platform.python_version()}")
        embed.set_footer(text="Run /help to see everything I can do")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="serverinfo", description="Stats and details about this server.")
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context):
        """Stats and details about this server."""
        guild = ctx.guild
        embed = info_embed("", title=guild.name)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="R"))
        embed.add_field(name="Members", value=f"{guild.member_count:,}")
        embed.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text · {len(guild.voice_channels)} voice · "
                  f"{len(guild.categories)} categories",
        )
        embed.add_field(name="Roles", value=str(len(guild.roles)))
        embed.add_field(name="Emojis", value=str(len(guild.emojis)))
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier} ({guild.premium_subscription_count} boosts)")
        embed.add_field(name="Verification", value=str(guild.verification_level).title())
        embed.set_footer(text=f"Server ID: {guild.id}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="userinfo", description="Details about a member: join date, roles, and more.")
    @app_commands.describe(member="Who to inspect (defaults to yourself).")
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Details about a member: account age, join date, roles, and more."""
        member = member or ctx.author
        roles = [r.mention for r in sorted(member.roles, key=lambda r: r.position, reverse=True) if r.name != "@everyone"]
        embed = info_embed("", title=f"{member.display_name} ({member})")
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, style="R"))
        if member.joined_at:
            embed.add_field(name="Joined server", value=discord.utils.format_dt(member.joined_at, style="R"))
        embed.add_field(name="Bot", value="Yes 🤖" if member.bot else "No")
        if member.premium_since:
            embed.add_field(name="Boosting since", value=discord.utils.format_dt(member.premium_since, style="R"))
        shown = roles[:15]
        overflow = f" … +{len(roles) - 15} more" if len(roles) > 15 else ""
        embed.add_field(name=f"Roles ({len(roles)})", value=(" ".join(shown) + overflow) if roles else "*none*", inline=False)
        embed.set_footer(text=f"User ID: {member.id}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="avatar", description="Show a member's avatar, full size.")
    @app_commands.describe(member="Whose avatar (defaults to yourself).")
    async def avatar(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show a member's avatar, full size."""
        target = member or ctx.author
        embed = info_embed(f"[Open original]({target.display_avatar.url})", title=f"{target.display_name}'s avatar")
        embed.set_image(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="remind", description="Set a reminder, e.g. /remind 1h30m check the oven.")
    @app_commands.describe(duration="When, e.g. 30m, 1h30m, 2d.", text="What to remind you about.")
    async def remind(self, ctx: commands.Context, duration: str, *, text: str):
        """Set a reminder. The bot pings you here when it fires (DM fallback).
        Survives bot restarts."""
        seconds = parse_duration(duration)
        if seconds is None or seconds < 10:
            await ctx.send(embed=error_embed(
                f"Couldn't understand `{duration}` — use something like `30m`, `1h30m`, or `2d` (min 10s)."
            ), ephemeral=True)
            return
        if seconds > MAX_REMINDER_SECONDS:
            await ctx.send(embed=error_embed("Reminders max out at 90 days."), ephemeral=True)
            return
        remind_at = time.time() + seconds
        self.bot.schedule_reminder(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else 0,
            channel_id=ctx.channel.id,
            remind_at=remind_at,
            text=text,
        )
        await ctx.send(embed=ok_embed(f"⏰ Got it — I'll remind you <t:{int(remind_at)}:R>: *{text}*"))

    @commands.hybrid_command(name="reminders", description="List your pending reminders, with the option to cancel.")
    async def reminders(self, ctx: commands.Context):
        """List your pending reminders, with a dropdown to cancel one."""
        mine = {rid: e for rid, e in self.bot.reminders.items() if e["user_id"] == ctx.author.id}
        if not mine:
            await ctx.send(embed=info_embed("You have no pending reminders."), ephemeral=True)
            return
        lines = [f"• <t:{int(e['remind_at'])}:R> — {e['text'][:80]}" for e in mine.values()]
        embed = info_embed("\n".join(lines), title=f"⏰ Your reminders ({len(mine)})")
        view = ReminderCancelView(self.bot, ctx.author.id, mine)
        view.message = await ctx.send(embed=embed, view=view, ephemeral=True)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="invites", description="List this server's active invites and their use counts.")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @commands.bot_has_permissions(manage_guild=True)
    @commands.guild_only()
    async def invites(self, ctx: commands.Context):
        """List this server's active invites, who created them, and use counts."""
        invites = await ctx.guild.invites()
        if not invites:
            await ctx.send(embed=info_embed("This server has no active invites."))
            return
        invites.sort(key=lambda i: i.uses or 0, reverse=True)
        pages: List[discord.Embed] = []
        for start in range(0, len(invites), 10):
            lines = []
            for inv in invites[start:start + 10]:
                inviter = inv.inviter.mention if inv.inviter else "*unknown*"
                lines.append(f"`{inv.code}` by {inviter} — **{inv.uses or 0}** use(s), {inv.channel.mention}")
            pages.append(info_embed("\n".join(lines), title=f"🔗 Active invites ({len(invites)})"))
        await Paginator.send(ctx, pages)


class ReminderCancelView(discord.ui.View):
    def __init__(self, bot: "BurgBot", author_id: int, reminders: dict):
        super().__init__(timeout=120)
        self.bot = bot
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.cancel_select.options = [
            discord.SelectOption(
                label=entry["text"][:90] or "(no text)",
                value=rid,
                description=f"in {format_duration(max(0, int(entry['remind_at'] - time.time())))}",
            )
            for rid, entry in list(reminders.items())[:25]
        ]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("These aren't your reminders.", ephemeral=True)
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    @discord.ui.select(placeholder="Cancel a reminder…")
    async def cancel_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        rid = select.values[0]
        if self.bot.cancel_reminder(rid):
            content = "Reminder cancelled."
        else:
            content = "That reminder already fired or was cancelled."
        remaining = {r: e for r, e in self.bot.reminders.items() if e["user_id"] == self.author_id}
        if remaining:
            lines = [f"• <t:{int(e['remind_at'])}:R> — {e['text'][:80]}" for e in remaining.values()]
            embed = info_embed("\n".join(lines), title=f"⏰ Your reminders ({len(remaining)})")
        else:
            embed = info_embed("You have no pending reminders.")
            for item in self.children:
                item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(content, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Utility(bot))
