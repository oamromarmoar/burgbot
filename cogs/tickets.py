"""
Ticket tooling: manual opener overrides, ticket inspection, and management of
scheduled referral-role removals. The detection/removal logic itself lives in
core.py (it's event-driven).
"""

from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import Paginator, error_embed, info_embed, ok_embed

if TYPE_CHECKING:
    from core import BurgBot


class Tickets(commands.Cog):
    """Ticket-channel tools and scheduled role-removal management."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    @commands.hybrid_command(
        name="set_ticket_opener",
        description="Manually record who opened a ticket, for cases automatic detection can't figure out.",
    )
    @app_commands.describe(channel="The ticket channel.", member="Who opened it.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def set_ticket_opener(self, ctx: commands.Context, channel: discord.TextChannel, member: discord.Member):
        """Manually record who opened a ticket, for channels the automatic
        detection can't figure out."""
        self.bot.ticket_openers[str(channel.id)] = str(member.id)
        self.bot.save_ticket_openers()
        await ctx.send(embed=ok_embed(f"Recorded {member.mention} as the opener of {channel.mention}."))

    @commands.hybrid_command(
        name="ticket_info",
        description="Show what the bot knows about a ticket: detection, opener, and any scheduled removal.",
    )
    @app_commands.describe(channel="The channel to inspect (defaults to the current one).")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def ticket_info(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None):
        """Show what the bot knows about a ticket channel: whether it's
        detected as a ticket, who the opener is, and any scheduled removal."""
        channel = channel or ctx.channel
        is_ticket = self.bot.is_ticket_channel(channel)
        embed = info_embed("", title=f"Ticket info — #{channel.name}")
        embed.add_field(name="Detected as ticket", value="✅ Yes" if is_ticket else "❌ No")

        opener = await self.bot.get_ticket_opener(channel) if is_ticket else None
        embed.add_field(name="Opener", value=opener.mention if opener else "*unknown*")

        removal = next(
            (e for e in self.bot.pending_removals.values() if e["channel_id"] == channel.id), None
        )
        embed.add_field(
            name="Scheduled role removal",
            value=f"<@{removal['member_id']}> — fires <t:{int(removal['remove_at'])}:R>" if removal else "*none*",
            inline=False,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="pending_removals", description="List all scheduled referral-role removals in this server."
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def pending_removals(self, ctx: commands.Context):
        """List all scheduled referral-role removals in this server."""
        entries = [e for e in self.bot.pending_removals.values() if e["guild_id"] == ctx.guild.id]
        if not entries:
            await ctx.send(embed=info_embed("No referral-role removals are currently scheduled."))
            return
        entries.sort(key=lambda e: e["remove_at"])
        pages = []
        for start in range(0, len(entries), 10):
            lines = [
                f"<@{e['member_id']}> in <#{e['channel_id']}> — fires <t:{int(e['remove_at'])}:R>"
                for e in entries[start:start + 10]
            ]
            pages.append(info_embed("\n".join(lines), title="⏳ Scheduled role removals"))
        await Paginator.send(ctx, pages)

    @commands.hybrid_command(
        name="cancel_removal",
        description="Cancel a scheduled referral-role removal for a ticket opener.",
    )
    @app_commands.describe(channel="The ticket channel the removal was scheduled from.", member="The ticket opener.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def cancel_removal(self, ctx: commands.Context, channel: discord.TextChannel, member: discord.Member):
        """Cancel a scheduled referral-role removal (e.g. the Chef replied by
        mistake, or staff decided the member should keep the role)."""
        key = f"{channel.id}:{member.id}"
        if self.bot.cancel_pending_removal(key):
            await self.bot.send_log(
                ctx.guild,
                f"🚫 {ctx.author.mention} cancelled the scheduled role removal for {member.mention} "
                f"({channel.mention}).",
            )
            await ctx.send(embed=ok_embed(
                f"Cancelled the scheduled role removal for {member.mention} in {channel.mention}."
            ))
        else:
            await ctx.send(embed=error_embed(
                f"No scheduled removal found for {member.mention} in {channel.mention}. "
                f"Use `/pending_removals` to see what's scheduled."
            ))


async def setup(bot):
    await bot.add_cog(Tickets(bot))
