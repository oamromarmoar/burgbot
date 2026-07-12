"""
Server configuration: the dropdown-driven /setup wizard (4 steps), a
/settings overview, and the owner-only !sync fallback.

The on-join "Start Setup" prompt is a *persistent* view (fixed custom_id,
registered on every startup), so the button keeps working across restarts.
"""

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    ACCOUNT_AGE_CHOICES,
    DEFAULT_GOODBYE_MESSAGE,
    DEFAULT_WELCOME_MESSAGE,
    DELAY_CHOICES,
    REFERRALS_NEEDED_CHOICES,
    RETENTION_CHOICES,
)
from ui import format_duration, info_embed

if TYPE_CHECKING:
    from core import BurgBot

log = logging.getLogger("burgbot.admin")

ADMIN_ONLY_MESSAGE = "You need the **Administrator** permission to do this."
TOTAL_STEPS = 4


def _role_display(guild: discord.Guild, role_id) -> str:
    if not role_id:
        return "*not set*"
    role = guild.get_role(int(role_id))
    return role.mention if role else f"`{role_id}` (role not found)"


def _channel_display(guild: discord.Guild, channel_id) -> str:
    if not channel_id:
        return "*not set*"
    channel = guild.get_channel(int(channel_id))
    return channel.mention if channel else f"`{channel_id}` (channel not found)"


def build_config_embed(guild: discord.Guild, config: dict, *, title: str, description: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.add_field(name="Referral role", value=_role_display(guild, config.get("referral_role_id")))
    embed.add_field(name="Chef role", value=_role_display(guild, config.get("chef_role_id")))
    embed.add_field(name="Ticket category", value=_channel_display(guild, config.get("ticket_category_id")))
    embed.add_field(name="Ticket name prefix", value=f"`{config.get('ticket_channel_prefix') or '(none)'}`")
    embed.add_field(name="Referrals needed", value=str(config.get("referrals_needed", 5)))
    embed.add_field(name="Role removal delay", value=format_duration(config.get("role_removal_delay_seconds")))
    embed.add_field(name="Min account age", value=format_duration(config.get("min_account_age_seconds")))
    embed.add_field(name="Min member retention", value=format_duration(config.get("min_member_retention_seconds")))
    embed.add_field(
        name="Suspicious-join log channel", value=_channel_display(guild, config.get("suspicious_log_channel_id"))
    )
    embed.add_field(name="Activity log channel", value=_channel_display(guild, config.get("log_channel_id")))
    embed.add_field(name="Welcome channel", value=_channel_display(guild, config.get("welcome_channel_id")))
    embed.add_field(name="Goodbye channel", value=_channel_display(guild, config.get("goodbye_channel_id")))
    return embed


def _build_setup_embed(guild: discord.Guild, config: dict, step: int) -> discord.Embed:
    return build_config_embed(
        guild, config,
        title=f"⚙️ Server setup ({step}/{TOTAL_STEPS})",
        description="Admins only. Pick values with the dropdowns below, then continue.",
    )


def _make_duration_options(choices, current) -> list:
    return [
        discord.SelectOption(label=label, value=str(seconds), default=(int(current or 0) == seconds))
        for label, seconds in choices
    ]


class AdminOnlyView(discord.ui.View):
    def __init__(self, bot: "BurgBot", guild: discord.Guild, config: dict, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.guild = guild
        self.config = config
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return False

    async def on_timeout(self):
        if self.message is None:
            return
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(content="Setup timed out — run `/setup` again to continue.", view=self)
        except (discord.Forbidden, discord.HTTPException):
            pass


class TicketPrefixModal(discord.ui.Modal, title="Ticket Channel Name Prefix"):
    prefix: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name must start with (blank = off)",
        placeholder="ticket-",
        required=False,
        max_length=50,
    )

    def __init__(self, step_view: "SetupStep1View"):
        super().__init__()
        self.step_view = step_view
        self.prefix.default = step_view.config.get("ticket_channel_prefix", "")

    async def on_submit(self, interaction: discord.Interaction):
        self.step_view.config["ticket_channel_prefix"] = self.prefix.value.strip()
        await interaction.response.edit_message(embed=self.step_view.render_embed(), view=self.step_view)


class WelcomeMessagesModal(discord.ui.Modal, title="Welcome / Goodbye Messages"):
    welcome: discord.ui.TextInput = discord.ui.TextInput(
        label="Welcome message (blank = reset to default)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="Placeholders: {mention} {name} {server} {count}",
    )
    goodbye: discord.ui.TextInput = discord.ui.TextInput(
        label="Goodbye message (blank = reset to default)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="Placeholders: {mention} {name} {server} {count}",
    )

    def __init__(self, step_view: "SetupStep4View"):
        super().__init__()
        self.step_view = step_view
        self.welcome.default = step_view.config.get("welcome_message", DEFAULT_WELCOME_MESSAGE)
        self.goodbye.default = step_view.config.get("goodbye_message", DEFAULT_GOODBYE_MESSAGE)

    async def on_submit(self, interaction: discord.Interaction):
        self.step_view.config["welcome_message"] = self.welcome.value.strip() or DEFAULT_WELCOME_MESSAGE
        self.step_view.config["goodbye_message"] = self.goodbye.value.strip() or DEFAULT_GOODBYE_MESSAGE
        await interaction.response.edit_message(embed=self.step_view.render_embed(), view=self.step_view)


class SetupStep1View(AdminOnlyView):
    """Step 1: roles + ticket detection."""

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 1)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Referral role (granted after enough referrals)",
        min_values=0, max_values=1, row=0,
    )
    async def referral_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.config["referral_role_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Chef role (triggers ticket role removal)",
        min_values=0, max_values=1, row=1,
    )
    async def chef_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.config["chef_role_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Ticket category (optional)",
        channel_types=[discord.ChannelType.category], min_values=0, max_values=1, row=2,
    )
    async def ticket_category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.config["ticket_category_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Set Ticket Name Prefix", style=discord.ButtonStyle.secondary, row=3)
    async def set_prefix_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketPrefixModal(self))

    @discord.ui.button(label="Next: Thresholds ▶", style=discord.ButtonStyle.primary, row=4)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep2View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)


class SetupStep2View(AdminOnlyView):
    """Step 2: thresholds + timing."""

    def __init__(self, bot, guild, config):
        super().__init__(bot, guild, config)
        self.referrals_needed_select.options = [
            discord.SelectOption(
                label=f"{n} referral(s)", value=str(n),
                default=(config.get("referrals_needed") == n),
            )
            for n in REFERRALS_NEEDED_CHOICES
        ]
        self.removal_delay_select.options = _make_duration_options(
            DELAY_CHOICES, config.get("role_removal_delay_seconds")
        )
        self.account_age_select.options = _make_duration_options(
            ACCOUNT_AGE_CHOICES, config.get("min_account_age_seconds")
        )
        self.retention_select.options = _make_duration_options(
            RETENTION_CHOICES, config.get("min_member_retention_seconds")
        )

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 2)

    @discord.ui.select(
        placeholder="Referrals needed for a reward",
        options=[discord.SelectOption(label="5 referral(s)", value="5")], row=0,
    )
    async def referrals_needed_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.config["referrals_needed"] = int(select.values[0])
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        placeholder="Delay before removing the role after a Chef reply",
        options=[discord.SelectOption(label="20 minutes", value="1200")], row=1,
    )
    async def removal_delay_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.config["role_removal_delay_seconds"] = int(select.values[0])
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        placeholder="Minimum account age to auto-credit a join",
        options=[discord.SelectOption(label="3 days", value="259200")], row=2,
    )
    async def account_age_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.config["min_account_age_seconds"] = int(select.values[0])
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        placeholder="Min time a referred member must stay before credit finalizes",
        options=[discord.SelectOption(label="24 hours", value="86400")], row=3,
    )
    async def retention_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.config["min_member_retention_seconds"] = int(select.values[0])
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=4)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep1View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)

    @discord.ui.button(label="Next: Logging ▶", style=discord.ButtonStyle.primary, row=4)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep3View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)


class SetupStep3View(AdminOnlyView):
    """Step 3: suspicious-join + activity logging."""

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 3)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Channel for suspicious-join alerts (optional)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=0,
    )
    async def suspicious_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.config["suspicious_log_channel_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Channel for general activity logs (optional)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=1,
    )
    async def log_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.config["log_channel_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep2View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)

    @discord.ui.button(label="Next: Welcome ▶", style=discord.ButtonStyle.primary, row=2)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep4View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)


class SetupStep4View(AdminOnlyView):
    """Step 4: welcome/goodbye messages + save."""

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 4)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Welcome message channel (optional)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=0,
    )
    async def welcome_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.config["welcome_channel_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Goodbye message channel (optional)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=1,
    )
    async def goodbye_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.config["goodbye_channel_id"] = select.values[0].id if select.values else 0
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.button(label="Edit Message Templates", style=discord.ButtonStyle.secondary, row=2)
    async def edit_messages_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WelcomeMessagesModal(self))

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=3)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep3View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)

    @discord.ui.button(label="✅ Save Configuration", style=discord.ButtonStyle.success, row=3)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.bot.update_guild_config(self.guild.id, **self.config)
        for item in self.children:
            item.disabled = True
        embed = self.render_embed()
        embed.color = discord.Color.green()
        embed.description = "✅ Configuration saved — takes effect immediately, no restart needed."
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


class SetupPromptView(discord.ui.View):
    """Posted on_guild_join. Persistent: the fixed custom_id + timeout=None +
    add_view() registration in cog_load keep the button working after
    restarts."""

    def __init__(self, bot: "BurgBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return False

    @discord.ui.button(
        label="Start Setup", style=discord.ButtonStyle.primary, emoji="⚙", custom_id="burgbot:start_setup"
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        config = dict(self.bot.get_guild_config(guild.id))
        view = SetupStep1View(self.bot, guild, config)
        await interaction.response.edit_message(content=None, embed=view.render_embed(), view=view)
        view.message = interaction.message


class Admin(commands.Cog):
    """Server setup and bot administration."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    async def cog_load(self):
        # Re-register the persistent on-join setup prompt after every restart.
        self.bot.add_view(SetupPromptView(self.bot))

    @commands.hybrid_command(
        name="setup", description="(Re)configure this server's settings via a dropdown wizard."
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def setup_command(self, ctx: commands.Context):
        """(Re)configure this server via the dropdown wizard: roles, ticket
        detection, thresholds, logging, and welcome messages."""
        config = dict(self.bot.get_guild_config(ctx.guild.id))
        view = SetupStep1View(self.bot, ctx.guild, config)
        view.message = await ctx.send(embed=view.render_embed(), view=view)

    @commands.hybrid_command(name="settings", description="Show this server's current configuration.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def settings(self, ctx: commands.Context):
        """Show this server's current effective configuration."""
        config = self.bot.get_guild_config(ctx.guild.id)
        configured = str(ctx.guild.id) in self.bot.guild_configs
        embed = build_config_embed(
            ctx.guild, config,
            title="⚙️ Current settings",
            description=(
                "Change anything with `/setup`."
                if configured
                else "⚠️ This server hasn't been set up yet — these are the defaults. Run `/setup`."
            ),
        )
        embed.add_field(name="Welcome message", value=config["welcome_message"][:200], inline=False)
        embed.add_field(name="Goodbye message", value=config["goodbye_message"][:200], inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync_command(self, ctx: commands.Context):
        """Force a re-sync of the slash-command tree with Discord, bypassing
        the unchanged-since-last-sync skip. Prefix-only (deliberately — if the
        tree is out of sync, this is the only way in). Normally unnecessary:
        the tree is synced automatically on startup."""
        try:
            count = await self.bot.sync_commands(force=True)
        except (discord.HTTPException, app_commands.AppCommandError, discord.ClientException) as exc:
            await ctx.send(f"Sync failed: {exc}")
            return
        await ctx.send(embed=info_embed(f"Synced {count} slash command(s)."))


async def setup(bot):
    await bot.add_cog(Admin(bot))
