"""
Referral commands: personal stats, leaderboard, invited-by lookup, manual
adjustments, and an interactive approve/deny review queue for flagged joins.
"""

import time
from typing import TYPE_CHECKING, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import Paginator, error_embed, info_embed, ok_embed, progress_bar, warn_embed

if TYPE_CHECKING:
    from core import BurgBot


class SuspiciousReviewView(discord.ui.View):
    """Pages through flagged joins one at a time with Approve/Deny buttons.
    Anyone with Manage Roles can act, not just the command invoker."""

    def __init__(self, bot: "BurgBot", guild: discord.Guild):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.index = 0
        self.message: Optional[discord.Message] = None

    def entries(self) -> List[dict]:
        return [e for e in self.bot.suspicious_joins.values() if e["guild_id"] == self.guild.id]

    def render(self) -> discord.Embed:
        entries = self.entries()
        if not entries:
            for item in self.children:
                item.disabled = True
            self.stop()
            return ok_embed("No suspicious joins pending review. 🎉", title="Review queue")
        self.index = min(self.index, len(entries) - 1)
        entry = entries[self.index]
        member = self.guild.get_member(entry["member_id"])
        embed = warn_embed(
            "Approve to credit the referrer, deny to discard without crediting anyone.",
            title=f"⚠️ Flagged join — {self.index + 1} of {len(entries)}",
        )
        embed.add_field(name="Member", value=f"<@{entry['member_id']}>")
        embed.add_field(name="Referred by", value=f"<@{entry['referrer_id']}>")
        embed.add_field(name="Account age at join", value=f"{entry['account_age_seconds'] / 3600:.1f}h")
        embed.add_field(name="Flagged", value=f"<t:{int(entry['flagged_at'])}:R>")
        embed.add_field(
            name="Still in server", value="✅ Yes" if member else "❌ No — approve is impossible, deny instead"
        )
        self.prev_entry.disabled = self.index == 0
        self.next_entry.disabled = self.index >= len(entries) - 1
        return embed

    def current_member_id(self) -> Optional[int]:
        entries = self.entries()
        if not entries:
            return None
        return entries[min(self.index, len(entries) - 1)]["member_id"]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_roles:
            return True
        await interaction.response.send_message(
            "You need the **Manage Roles** permission to review flagged joins.", ephemeral=True
        )
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    async def _resolve(self, interaction: discord.Interaction, approve: bool):
        member_id = self.current_member_id()
        if member_id is None:
            await interaction.response.edit_message(embed=self.render(), view=self)
            return
        if approve:
            ok, result = await self.bot.approve_suspicious_join(self.guild, member_id, interaction.user)
        else:
            ok, result = await self.bot.deny_suspicious_join(self.guild, member_id, interaction.user)
        await interaction.response.edit_message(embed=self.render(), view=self)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_entry(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index += 1
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, approve=True)

    @discord.ui.button(label="Deny", emoji="❌", style=discord.ButtonStyle.danger, row=1)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, approve=False)


class Referrals(commands.Cog):
    """Referral tracking: stats, leaderboard, and staff review tools."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="referrals",
        aliases=["referrals_count"],
        description="Show a member's referral progress, lifetime total, and rank.",
    )
    @app_commands.describe(member="Whose referrals to check (defaults to yourself).")
    @commands.guild_only()
    async def referrals(self, ctx: commands.Context, member: Optional[discord.Member] = None):
        """Show a member's referral progress, lifetime total, and rank."""
        target = member or ctx.author
        cfg = self.bot.get_guild_config(ctx.guild.id)
        progress = self.bot.get_progress(ctx.guild.id, target.id)
        total = self.bot.get_total(ctx.guild.id, target.id)

        totals = self.bot.total_referrals.get(str(ctx.guild.id), {})
        rank = 1 + sum(1 for count in totals.values() if count > total)

        embed = info_embed("", title=f"Referral stats — {target.display_name}")
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(
            name=f"Progress toward next reward",
            value=progress_bar(progress, cfg["referrals_needed"]),
            inline=False,
        )
        embed.add_field(name="Lifetime referrals", value=f"**{total}**")
        embed.add_field(name="Server rank", value=f"**#{rank}**" if total else "*unranked*")
        referrer_id = self.bot.credited_members.get(str(target.id))
        if referrer_id:
            embed.add_field(name="Invited by", value=f"<@{referrer_id}>")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="leaderboard", description="Top referrers in this server.")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        """Top referrers in this server, by lifetime referrals."""
        totals = self.bot.total_referrals.get(str(ctx.guild.id), {})
        ranked = sorted(
            ((uid, count) for uid, count in totals.items() if count > 0),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if not ranked:
            await ctx.send(embed=info_embed("Nobody has any referrals yet — invite some friends!"))
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        pages = []
        for start in range(0, len(ranked), 10):
            lines = []
            for offset, (uid, count) in enumerate(ranked[start:start + 10]):
                place = start + offset + 1
                marker = medals.get(place, f"`#{place}`")
                lines.append(f"{marker} <@{uid}> — **{count}** referral(s)")
            embed = info_embed("\n".join(lines), title=f"🏆 Referral leaderboard — {ctx.guild.name}")
            pages.append(embed)
        await Paginator.send(ctx, pages)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="who_invited", description="Look up who referred a member, and the credit's status.")
    @app_commands.describe(member="The member to look up.")
    @commands.guild_only()
    async def who_invited(self, ctx: commands.Context, member: discord.Member):
        """Look up who referred a member, and whether the credit is finalized,
        pending, or awaiting review."""
        key = str(member.id)
        if key in self.bot.credited_members:
            await ctx.send(embed=info_embed(
                f"{member.mention} was invited by <@{self.bot.credited_members[key]}> (credit finalized)."
            ))
        elif key in self.bot.pending_credits:
            entry = self.bot.pending_credits[key]
            await ctx.send(embed=info_embed(
                f"{member.mention} was invited by <@{entry['referrer_id']}> — credit is pending and "
                f"finalizes <t:{int(entry['credit_at'])}:R> if they stay."
            ))
        elif key in self.bot.suspicious_joins:
            entry = self.bot.suspicious_joins[key]
            await ctx.send(embed=warn_embed(
                f"{member.mention} was invited by <@{entry['referrer_id']}> — flagged for account age "
                f"and awaiting staff review (`/suspicious_joins`)."
            ))
        else:
            await ctx.send(embed=info_embed(
                f"No referral record for {member.mention} (joined before tracking, via a vanity URL, "
                f"or the invite couldn't be attributed)."
            ))

    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="suspicious_joins",
        description="Review joins flagged for account age, with approve/deny buttons.",
    )
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def suspicious_joins(self, ctx: commands.Context):
        """Review joins flagged for account age, with approve/deny buttons."""
        view = SuspiciousReviewView(self.bot, ctx.guild)
        view.message = await ctx.send(embed=view.render(), view=view)

    @commands.hybrid_command(
        name="approve_referral", description="Manually credit a flagged join after staff confirms it's legitimate."
    )
    @app_commands.describe(member="The flagged member to approve.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def approve_referral(self, ctx: commands.Context, member: discord.Member):
        """Manually credit a flagged join after staff confirms it's legitimate."""
        ok, result = await self.bot.approve_suspicious_join(ctx.guild, member.id, ctx.author)
        await ctx.send(embed=(ok_embed(result) if ok else error_embed(result)))

    @commands.hybrid_command(name="deny_referral", description="Discard a flagged join without crediting anyone.")
    @app_commands.describe(member="The flagged member to deny.")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def deny_referral(self, ctx: commands.Context, member: discord.Member):
        """Discard a flagged join without crediting anyone."""
        ok, result = await self.bot.deny_suspicious_join(ctx.guild, member.id, ctx.author)
        await ctx.send(embed=(ok_embed(result) if ok else error_embed(result)))

    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="add_referral",
        description="Manually add to a member's referral count; grants the role if the threshold is crossed.",
    )
    @app_commands.describe(member="Who to credit.", amount="How many referrals to add (default 1).")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def add_referral(self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1, 1000] = 1):
        """Manually add to a member's referral count (progress + lifetime).
        Grants the referral role if this crosses the threshold."""
        cfg = self.bot.get_guild_config(ctx.guild.id)
        progress, total = self.bot.adjust_referral_counts(ctx.guild.id, member.id, amount)

        await self.bot.send_log(
            ctx.guild,
            f"➕ {ctx.author.mention} manually added {amount} referral(s) to {member.mention} "
            f"({progress}/{cfg['referrals_needed']} progress, {total} lifetime).",
        )
        await ctx.send(embed=ok_embed(
            f"Added **{amount}** referral(s) to {member.mention}.\n"
            f"{progress_bar(progress, cfg['referrals_needed'])} toward next reward — {total} lifetime."
        ))

        if cfg["referral_role_id"] and progress >= cfg["referrals_needed"]:
            await self.bot.grant_referral_role(ctx.guild, member.id)

    @commands.hybrid_command(
        name="remove_referral",
        description="Manually remove from a member's referral count (floored at 0). Doesn't revoke a granted role.",
    )
    @app_commands.describe(member="Who to deduct from.", amount="How many referrals to remove (default 1).")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.guild_only()
    async def remove_referral(self, ctx: commands.Context, member: discord.Member, amount: commands.Range[int, 1, 1000] = 1):
        """Manually remove from a member's referral count (progress + lifetime,
        floored at 0). Does not revoke an already-granted role — use Discord's
        own role management for that."""
        cfg = self.bot.get_guild_config(ctx.guild.id)
        progress, total = self.bot.adjust_referral_counts(ctx.guild.id, member.id, -amount)

        await self.bot.send_log(
            ctx.guild,
            f"➖ {ctx.author.mention} manually removed {amount} referral(s) from {member.mention} "
            f"({progress}/{cfg['referrals_needed']} progress, {total} lifetime).",
            color=discord.Color.orange(),
        )
        await ctx.send(embed=ok_embed(
            f"Removed **{amount}** referral(s) from {member.mention}.\n"
            f"{progress_bar(progress, cfg['referrals_needed'])} toward next reward — {total} lifetime."
        ))


async def setup(bot):
    await bot.add_cog(Referrals(bot))
