"""
Invite supervision: look up who invited a member, and whom a member has
invited. Joins are attributed and logged in core.py; nothing is scored.
"""

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ui import Paginator, info_embed

if TYPE_CHECKING:
    from core import BurgBot


class Invites(commands.Cog):
    """Who-invited-whom lookups for staff supervision."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    @commands.hybrid_command(name="who_invited", description="Look up whose invite a member joined through.")
    @app_commands.describe(member="The member to look up.")
    @commands.guild_only()
    async def who_invited(self, ctx: commands.Context, member: discord.Member):
        """Look up whose invite a member joined through."""
        inviter_id = self.bot.invited_by.get(str(member.id))
        if inviter_id:
            await ctx.send(embed=info_embed(f"{member.mention} was invited by <@{inviter_id}>."))
        else:
            await ctx.send(embed=info_embed(
                f"No invite record for {member.mention} (joined before tracking, via a vanity URL, "
                f"or the invite couldn't be attributed)."
            ))

    @commands.hybrid_command(name="invited_list", description="List the members someone has invited.")
    @app_commands.describe(member="Whose invitees to list.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def invited_list(self, ctx: commands.Context, member: discord.Member):
        """List the current server members who joined through a member's
        invites (members who have since left aren't shown)."""
        inviter_key = str(member.id)
        invitees = [
            ctx.guild.get_member(int(member_id))
            for member_id, inviter_id in self.bot.invited_by.items()
            if inviter_id == inviter_key
        ]
        invitees = [m for m in invitees if m is not None]
        if not invitees:
            await ctx.send(embed=info_embed(f"No current members joined through {member.mention}'s invites."))
            return
        pages = []
        for start in range(0, len(invitees), 15):
            lines = [f"• {m.mention}" for m in invitees[start:start + 15]]
            pages.append(info_embed(
                "\n".join(lines), title=f"📨 Invited by {member.display_name} ({len(invitees)})"
            ))
        await Paginator.send(ctx, pages)


async def setup(bot):
    await bot.add_cog(Invites(bot))
