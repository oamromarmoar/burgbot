"""
Welcome / goodbye messages. Channels and message templates are configured in
/setup (step 4). Templates support {mention}, {name}, {server}, and {count}
placeholders; unknown placeholders are left as-is rather than crashing.
"""

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ui import COLOR_OK, COLOR_WARN, info_embed

if TYPE_CHECKING:
    from core import BurgBot

log = logging.getLogger("burgbot.welcome")


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def render_template(template: str, member: discord.Member) -> str:
    return template.format_map(_SafeDict(
        mention=member.mention,
        name=member.display_name,
        server=member.guild.name,
        count=member.guild.member_count,
    ))


class Welcome(commands.Cog):
    """Configurable welcome and goodbye messages."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    async def _post(self, member: discord.Member, channel_key: str, message_key: str, color):
        cfg = self.bot.get_guild_config(member.guild.id)
        channel_id = cfg[channel_key]
        if not channel_id:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(description=render_template(cfg[message_key], member), color=color)
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Failed to post %s message in guild %s: %s", message_key, member.guild.id, exc)

    @commands.Cog.listener("on_member_join")
    async def welcome_on_join(self, member: discord.Member):
        await self._post(member, "welcome_channel_id", "welcome_message", COLOR_OK)

    @commands.Cog.listener("on_member_remove")
    async def goodbye_on_remove(self, member: discord.Member):
        await self._post(member, "goodbye_channel_id", "goodbye_message", COLOR_WARN)

    @commands.hybrid_command(
        name="test_welcome", description="Preview this server's welcome and goodbye messages, using you as the member."
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def test_welcome(self, ctx: commands.Context):
        """Preview this server's welcome and goodbye messages, rendered as if
        you had just joined/left. Posts in the configured channels."""
        cfg = self.bot.get_guild_config(ctx.guild.id)
        if not cfg["welcome_channel_id"] and not cfg["goodbye_channel_id"]:
            await ctx.send(embed=info_embed(
                "No welcome or goodbye channel is configured yet — run `/setup` (step 4) first."
            ))
            return
        await self._post(ctx.author, "welcome_channel_id", "welcome_message", COLOR_OK)
        await self._post(ctx.author, "goodbye_channel_id", "goodbye_message", COLOR_WARN)
        await ctx.send(embed=info_embed("Sent previews to the configured channel(s)."), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Welcome(bot))
