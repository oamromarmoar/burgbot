"""
Shared UI helpers: consistent embed styling, progress bars, human-friendly
durations, and a reusable button paginator.
"""

import re
from typing import List, Optional

import discord

# One palette for the whole bot, so every reply is visually consistent.
COLOR_INFO = discord.Color.blurple()
COLOR_OK = discord.Color.green()
COLOR_WARN = discord.Color.orange()
COLOR_ERROR = discord.Color.red()
COLOR_FUN = discord.Color.gold()


def info_embed(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_INFO)


def ok_embed(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_OK)


def warn_embed(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_WARN)


def error_embed(description: str, *, title: Optional[str] = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_ERROR)


def progress_bar(current: int, needed: int, slots: int = 10) -> str:
    """Render e.g. `▰▰▰▱▱▱▱▱▱▱ 3/10`."""
    needed = max(needed, 1)
    filled = min(slots, round(slots * min(current, needed) / needed))
    return "▰" * filled + "▱" * (slots - filled) + f"  **{current}/{needed}**"


def vote_bar(count: int, total: int, slots: int = 12) -> str:
    filled = 0 if total == 0 else round(slots * count / total)
    pct = 0 if total == 0 else round(100 * count / total)
    return "█" * filled + "░" * (slots - filled) + f"  {count} ({pct}%)"


def format_duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Disabled"
    parts = []
    for label, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append(f"{seconds // size}{label}")
            seconds %= size
    return " ".join(parts)


_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.IGNORECASE)
_DURATION_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> Optional[int]:
    """Parse "1h30m", "2d", "45s", "1w" (or a bare number of minutes) into
    seconds. Returns None if nothing parseable is found."""
    text = text.strip()
    if text.isdigit():
        return int(text) * 60
    matches = _DURATION_RE.findall(text)
    if not matches or _DURATION_RE.sub("", text).strip():
        return None
    return sum(int(amount) * _DURATION_UNITS[unit.lower()] for amount, unit in matches)


class Paginator(discord.ui.View):
    """Button paginator over a prepared list of embeds. Only the invoker can
    turn pages; buttons disable themselves on timeout."""

    def __init__(self, embeds: List[discord.Embed], author_id: int, *, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.embeds = embeds
        self.author_id = author_id
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    @classmethod
    async def send(cls, ctx, embeds: List[discord.Embed], *, ephemeral: bool = False):
        """Send `embeds` as pages; skips the whole view when there is only one."""
        for i, embed in enumerate(embeds):
            embed.set_footer(text=f"Page {i + 1}/{len(embeds)}")
        if len(embeds) == 1:
            await ctx.send(embed=embeds[0], ephemeral=ephemeral)
            return
        view = cls(embeds, ctx.author.id)
        view.message = await ctx.send(embed=embeds[0], view=view, ephemeral=ephemeral)

    def _sync_buttons(self):
        self.first_page.disabled = self.prev_page.disabled = self.index == 0
        self.next_page.disabled = self.last_page.disabled = self.index >= len(self.embeds) - 1
        self.counter.label = f"{self.index + 1}/{len(self.embeds)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran the command can turn these pages.", ephemeral=True
        )
        return False

    async def _show(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embeds[self.index], view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.primary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.embeds) - 1, self.index + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = len(self.embeds) - 1
        await self._show(interaction)
