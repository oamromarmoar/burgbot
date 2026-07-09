"""
Moderation: purge, kick, ban/unban, timeouts, slowmode, and a persisted
warning system. Every action is posted to the activity log channel and the
target is DM'd (best effort) so punishments never happen silently.
"""

import datetime
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import Paginator, error_embed, format_duration, info_embed, ok_embed, parse_duration, warn_embed

if TYPE_CHECKING:
    from core import BurgBot

MAX_TIMEOUT_SECONDS = 28 * 24 * 3600  # Discord's hard limit


class Moderation(commands.Cog):
    """Moderation tools: purge, kick, ban, timeout, slowmode, warnings."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    # ------------------------------------------------------------------
    def _hierarchy_error(self, ctx: commands.Context, member: discord.Member) -> Optional[str]:
        if member.id == ctx.author.id:
            return "You can't moderate yourself."
        if member.id == ctx.bot.user.id:
            return "I'm flattered, but no."
        if member.id == ctx.guild.owner_id:
            return "The server owner can't be moderated."
        if ctx.guild.owner_id != ctx.author.id and member.top_role >= ctx.author.top_role:
            return "You can't moderate someone with a role equal to or above your own."
        if member.top_role >= ctx.guild.me.top_role:
            return "My top role isn't high enough to act on that member — move my role up in Server Settings → Roles."
        return None

    @staticmethod
    async def _notify_target(member: discord.abc.User, guild: discord.Guild, text: str):
        try:
            await member.send(embed=warn_embed(text, title=f"Moderation notice — {guild.name}"))
        except (discord.Forbidden, discord.HTTPException):
            pass  # DMs closed; the action still proceeds

    async def _ephemeral_reply(self, ctx: commands.Context, embed: discord.Embed):
        """Ephemeral for slash; short-lived for prefix (keeps channels clean)."""
        if ctx.interaction:
            await ctx.send(embed=embed, ephemeral=True)
        else:
            await ctx.send(embed=embed, delete_after=8)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="purge", description="Bulk-delete recent messages, optionally from one member only.")
    @app_commands.describe(amount="How many messages to scan/delete (1–100).", member="Only delete this member's messages.")
    @app_commands.default_permissions(manage_messages=True)
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True, read_message_history=True)
    @commands.guild_only()
    async def purge(
        self, ctx: commands.Context, amount: commands.Range[int, 1, 100], member: Optional[discord.Member] = None
    ):
        """Bulk-delete up to 100 recent messages in this channel, optionally
        only those from a specific member. Messages older than 14 days can't
        be bulk-deleted (Discord limitation)."""
        if ctx.interaction:
            await ctx.defer(ephemeral=True)
        else:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        check = (lambda m: m.author.id == member.id) if member else (lambda m: True)
        deleted = await ctx.channel.purge(limit=amount, check=check)

        who = f" from {member.mention}" if member else ""
        await self._ephemeral_reply(ctx, ok_embed(f"Deleted **{len(deleted)}** message(s){who}."))
        await self.bot.send_log(
            ctx.guild,
            f"🧹 {ctx.author.mention} purged {len(deleted)} message(s){who} in {ctx.channel.mention}.",
        )

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(member="Who to kick.", reason="Why (shown in the audit log and DM'd to them).")
    @app_commands.default_permissions(kick_members=True)
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    @commands.guild_only()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason given"):
        """Kick a member from the server."""
        problem = self._hierarchy_error(ctx, member)
        if problem:
            await ctx.send(embed=error_embed(problem), ephemeral=True)
            return
        await self._notify_target(member, ctx.guild, f"You were **kicked**.\n**Reason:** {reason}")
        await member.kick(reason=f"{ctx.author} — {reason}")
        await ctx.send(embed=ok_embed(f"👢 Kicked {member.mention}.\n**Reason:** {reason}"))
        await self.bot.send_log(
            ctx.guild, f"👢 {ctx.author.mention} kicked {member.mention}. Reason: {reason}",
            color=discord.Color.orange(),
        )

    @commands.hybrid_command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(
        member="Who to ban.",
        reason="Why (shown in the audit log and DM'd to them).",
        delete_days="Also delete their messages from the last N days (0–7).",
    )
    @app_commands.default_permissions(ban_members=True)
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def ban(
        self,
        ctx: commands.Context,
        member: discord.Member,
        delete_days: commands.Range[int, 0, 7] = 0,
        *,
        reason: str = "No reason given",
    ):
        """Ban a member, optionally deleting their recent messages."""
        problem = self._hierarchy_error(ctx, member)
        if problem:
            await ctx.send(embed=error_embed(problem), ephemeral=True)
            return
        await self._notify_target(member, ctx.guild, f"You were **banned**.\n**Reason:** {reason}")
        await member.ban(reason=f"{ctx.author} — {reason}", delete_message_seconds=delete_days * 86400)
        await ctx.send(embed=ok_embed(f"🔨 Banned {member.mention}.\n**Reason:** {reason}"))
        await self.bot.send_log(
            ctx.guild, f"🔨 {ctx.author.mention} banned {member.mention}. Reason: {reason}",
            color=discord.Color.red(),
        )

    @commands.hybrid_command(name="unban", description="Unban a previously banned user.")
    @app_commands.describe(user="The banned user (pick from the list, or paste their ID).")
    @app_commands.default_permissions(ban_members=True)
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def unban(self, ctx: commands.Context, user: discord.User):
        """Unban a previously banned user (mention, name, or raw ID)."""
        try:
            await ctx.guild.unban(user, reason=f"Unbanned by {ctx.author}")
        except discord.NotFound:
            await ctx.send(embed=error_embed(f"{user.mention} isn't banned."))
            return
        await ctx.send(embed=ok_embed(f"♻️ Unbanned {user.mention}."))
        await self.bot.send_log(ctx.guild, f"♻️ {ctx.author.mention} unbanned {user.mention}.")

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="timeout", description="Time a member out (mute) for a duration like 10m, 2h, 1d.")
    @app_commands.describe(member="Who to time out.", duration="How long, e.g. 10m, 2h, 1d (max 28d).", reason="Why.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def timeout(
        self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason given"
    ):
        """Time a member out for a duration like `10m`, `2h`, `1d` (max 28d)."""
        problem = self._hierarchy_error(ctx, member)
        if problem:
            await ctx.send(embed=error_embed(problem), ephemeral=True)
            return
        seconds = parse_duration(duration)
        if seconds is None or seconds <= 0:
            await ctx.send(embed=error_embed(
                f"Couldn't understand `{duration}` — use something like `30s`, `10m`, `2h`, or `1d`."
            ), ephemeral=True)
            return
        seconds = min(seconds, MAX_TIMEOUT_SECONDS)
        await member.timeout(datetime.timedelta(seconds=seconds), reason=f"{ctx.author} — {reason}")
        await self._notify_target(
            member, ctx.guild, f"You were **timed out** for {format_duration(seconds)}.\n**Reason:** {reason}"
        )
        await ctx.send(embed=ok_embed(
            f"🔇 Timed out {member.mention} for **{format_duration(seconds)}**.\n**Reason:** {reason}"
        ))
        await self.bot.send_log(
            ctx.guild,
            f"🔇 {ctx.author.mention} timed out {member.mention} for {format_duration(seconds)}. Reason: {reason}",
            color=discord.Color.orange(),
        )

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout early.")
    @app_commands.describe(member="Whose timeout to lift.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def untimeout(self, ctx: commands.Context, member: discord.Member):
        """Remove a member's timeout early."""
        if not member.is_timed_out():
            await ctx.send(embed=info_embed(f"{member.mention} isn't timed out."))
            return
        await member.timeout(None, reason=f"Timeout lifted by {ctx.author}")
        await ctx.send(embed=ok_embed(f"🔊 Lifted the timeout on {member.mention}."))
        await self.bot.send_log(ctx.guild, f"🔊 {ctx.author.mention} lifted the timeout on {member.mention}.")

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="slowmode", description="Set this channel's slowmode delay (0 to disable).")
    @app_commands.describe(seconds="Delay between messages per user, in seconds (0–21600).", channel="Defaults to here.")
    @app_commands.default_permissions(manage_channels=True)
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.guild_only()
    async def slowmode(
        self,
        ctx: commands.Context,
        seconds: commands.Range[int, 0, 21600],
        channel: Optional[discord.TextChannel] = None,
    ):
        """Set a channel's slowmode delay in seconds (0 disables it)."""
        channel = channel or ctx.channel
        await channel.edit(reason=f"Slowmode set by {ctx.author}", slowmode_delay=seconds)
        if seconds:
            await ctx.send(embed=ok_embed(f"🐢 Slowmode in {channel.mention} set to **{format_duration(seconds)}**."))
        else:
            await ctx.send(embed=ok_embed(f"🐇 Slowmode disabled in {channel.mention}."))

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="warn", description="Warn a member. Warnings are recorded and DM'd to them.")
    @app_commands.describe(member="Who to warn.", reason="Why — required, and DM'd to them.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        """Warn a member. The warning is persisted and DM'd to them."""
        problem = self._hierarchy_error(ctx, member)
        if problem:
            await ctx.send(embed=error_embed(problem), ephemeral=True)
            return
        count = self.bot.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
        await self._notify_target(
            member, ctx.guild, f"You received a **warning** (#{count}).\n**Reason:** {reason}"
        )
        await ctx.send(embed=warn_embed(f"⚠️ Warned {member.mention} (warning **#{count}**).\n**Reason:** {reason}"))
        await self.bot.send_log(
            ctx.guild, f"⚠️ {ctx.author.mention} warned {member.mention} (#{count}). Reason: {reason}",
            color=discord.Color.orange(),
        )

    @commands.hybrid_command(name="warnings", description="List a member's recorded warnings.")
    @app_commands.describe(member="Whose warnings to list.")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        """List a member's recorded warnings."""
        entries = self.bot.get_warnings(ctx.guild.id, member.id)
        if not entries:
            await ctx.send(embed=info_embed(f"{member.mention} has no warnings. 🎉"))
            return
        pages = []
        for start in range(0, len(entries), 5):
            embed = warn_embed("", title=f"Warnings — {member.display_name} ({len(entries)} total)")
            for i, entry in enumerate(entries[start:start + 5], start=start + 1):
                embed.add_field(
                    name=f"#{i}",
                    value=f"{entry['reason']}\n*by <@{entry['mod_id']}>, <t:{int(entry['at'])}:R>*",
                    inline=False,
                )
            pages.append(embed)
        await Paginator.send(ctx, pages)

    @commands.hybrid_command(name="unwarn", description="Remove one of a member's warnings (latest by default).")
    @app_commands.describe(member="Whose warning to remove.", index="Warning number from /warnings (default: latest).")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def unwarn(self, ctx: commands.Context, member: discord.Member, index: Optional[int] = None):
        """Remove one of a member's warnings — the latest, or `#index` as
        numbered in `/warnings`."""
        removed = self.bot.remove_warning(ctx.guild.id, member.id, index)
        if removed is None:
            await ctx.send(embed=error_embed(
                f"Nothing removed — check `/warnings {member.display_name}` for valid numbers."
            ))
            return
        remaining = len(self.bot.get_warnings(ctx.guild.id, member.id))
        await ctx.send(embed=ok_embed(
            f"Removed a warning from {member.mention} (*{removed['reason']}*). {remaining} remaining."
        ))
        await self.bot.send_log(
            ctx.guild, f"♻️ {ctx.author.mention} removed a warning from {member.mention} ({remaining} remaining)."
        )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
