"""
Fun & engagement: button-based polls (restart-safe), dice, coin flips,
magic 8-ball, and a decision maker.
"""

import asyncio
import logging
import random
import re
import time
import uuid
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import COLOR_FUN, error_embed, info_embed, parse_duration, vote_bar

if TYPE_CHECKING:
    from core import BurgBot

log = logging.getLogger("burgbot.fun")

MAX_POLL_SECONDS = 7 * 24 * 3600

EIGHT_BALL_ANSWERS = [
    "It is certain.", "Without a doubt.", "Yes — definitely.", "Most likely.",
    "Signs point to yes.", "Ask again later.", "Better not tell you now.",
    "Cannot predict now.", "Don't count on it.", "My reply is no.",
    "My sources say no.", "Outlook not so good.", "Very doubtful.",
]

_DICE_RE = re.compile(r"^(?P<count>\d*)d(?P<sides>\d+)(?:\s*\+\s*(?P<bonus>\d+))?$", re.IGNORECASE)


def build_poll_embed(poll: dict, *, closed: bool = False) -> discord.Embed:
    votes = poll["votes"]
    total = len(votes)
    counts = [0] * len(poll["options"])
    for choice in votes.values():
        if 0 <= choice < len(counts):
            counts[choice] += 1
    lines = []
    winner = max(counts) if counts else 0
    for option, count in zip(poll["options"], counts):
        crown = " 👑" if closed and total and count == winner else ""
        lines.append(f"**{option}**{crown}\n{vote_bar(count, total)}")
    embed = discord.Embed(title=f"📊 {poll['question']}", description="\n\n".join(lines), color=COLOR_FUN)
    if closed:
        embed.set_footer(text=f"Poll closed • {total} vote(s)")
    elif poll.get("end_at"):
        embed.description += f"\n\nEnds <t:{int(poll['end_at'])}:R>"
        embed.set_footer(text=f"{total} vote(s) • click a button to vote (you can change your vote)")
    else:
        embed.set_footer(text=f"{total} vote(s) • click a button to vote (you can change your vote)")
    return embed


class PollOptionButton(discord.ui.Button):
    def __init__(self, cog: "Fun", poll_id: str, idx: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=label[:40],
            custom_id=f"burgbot:poll:{poll_id}:{idx}",
            row=idx // 3,
        )
        self.cog = cog
        self.poll_id = poll_id
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        poll = self.cog.bot.polls.get(self.poll_id)
        if poll is None:
            await interaction.response.send_message("This poll has already ended.", ephemeral=True)
            return
        poll["votes"][str(interaction.user.id)] = self.idx
        self.cog.bot.save_polls()
        await interaction.response.edit_message(embed=build_poll_embed(poll))


class PollEndButton(discord.ui.Button):
    def __init__(self, cog: "Fun", poll_id: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="End poll",
            emoji="🛑",
            custom_id=f"burgbot:poll:{poll_id}:end",
            row=2,
        )
        self.cog = cog
        self.poll_id = poll_id

    async def callback(self, interaction: discord.Interaction):
        poll = self.cog.bot.polls.get(self.poll_id)
        if poll is None:
            await interaction.response.send_message("This poll has already ended.", ephemeral=True)
            return
        is_author = interaction.user.id == poll["author_id"]
        can_moderate = (
            isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.manage_messages
        )
        if not (is_author or can_moderate):
            await interaction.response.send_message(
                "Only the poll's creator (or someone with Manage Messages) can end it.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self.cog.end_poll(self.poll_id)


class PollView(discord.ui.View):
    """Persistent (timeout=None + fixed custom_ids): votes keep working after
    a bot restart because cog_load re-registers a view per stored poll."""

    def __init__(self, cog: "Fun", poll_id: str, options: list):
        super().__init__(timeout=None)
        for idx, option in enumerate(options):
            self.add_item(PollOptionButton(cog, poll_id, idx, option))
        self.add_item(PollEndButton(cog, poll_id))


class Fun(commands.Cog):
    """Polls and party-trick commands."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot
        self._poll_tasks: dict[str, asyncio.Task] = {}

    async def cog_load(self):
        # Revive polls that were open when the bot last shut down.
        for poll_id, poll in list(self.bot.polls.items()):
            self.bot.add_view(PollView(self, poll_id, poll["options"]), message_id=poll["message_id"])
            if poll.get("end_at"):
                self._arm_poll_end(poll_id, poll)

    async def cog_unload(self):
        for task in self._poll_tasks.values():
            task.cancel()
        self._poll_tasks.clear()

    def _arm_poll_end(self, poll_id: str, poll: dict):
        delay = max(0.0, poll["end_at"] - time.time())
        self._poll_tasks[poll_id] = asyncio.create_task(self._poll_end_worker(poll_id, delay))

    async def _poll_end_worker(self, poll_id: str, delay: float):
        try:
            await asyncio.sleep(delay)
            await self.end_poll(poll_id)
        finally:
            self._poll_tasks.pop(poll_id, None)

    async def end_poll(self, poll_id: str):
        poll = self.bot.polls.pop(poll_id, None)
        if poll is None:
            return
        self.bot.save_polls()
        task = self._poll_tasks.pop(poll_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        channel = self.bot.get_channel(poll["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(poll["message_id"])
            await message.edit(embed=build_poll_embed(poll, closed=True), view=None)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            log.warning("Could not finalize poll %s: %s", poll_id, exc)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="poll", description="Start a button poll with up to 5 options.")
    @app_commands.describe(
        question="What are you asking?",
        option1="First choice.",
        option2="Second choice.",
        option3="Third choice (optional).",
        option4="Fourth choice (optional).",
        option5="Fifth choice (optional).",
        duration="Auto-close after e.g. 1h or 2d (optional — open until ended otherwise).",
    )
    @commands.guild_only()
    @commands.cooldown(2, 30, commands.BucketType.user)
    async def poll(
        self,
        ctx: commands.Context,
        question: str,
        option1: str,
        option2: str,
        option3: Optional[str] = None,
        option4: Optional[str] = None,
        option5: Optional[str] = None,
        duration: Optional[str] = None,
    ):
        """Start a button poll with 2–5 options. Voters can change their vote;
        polls survive bot restarts. Prefix usage: quote each part —
        `!poll "Lunch?" "Burgers" "Pizza" "Salad" 1h`"""
        options = [opt for opt in (option1, option2, option3, option4, option5) if opt]
        end_at = None
        if duration:
            seconds = parse_duration(duration)
            if seconds is None or seconds < 60:
                await ctx.send(embed=error_embed(
                    f"Couldn't understand the duration `{duration}` — use e.g. `30m`, `2h`, `1d` (min 1 minute)."
                ), ephemeral=True)
                return
            end_at = time.time() + min(seconds, MAX_POLL_SECONDS)

        poll_id = uuid.uuid4().hex[:12]
        poll = {
            "guild_id": ctx.guild.id,
            "channel_id": ctx.channel.id,
            "message_id": 0,  # filled in right after sending
            "author_id": ctx.author.id,
            "question": question[:250],
            "options": [opt[:100] for opt in options],
            "votes": {},
            "end_at": end_at,
            "created_at": time.time(),
        }
        view = PollView(self, poll_id, poll["options"])
        message = await ctx.send(embed=build_poll_embed(poll), view=view)
        poll["message_id"] = message.id
        self.bot.polls[poll_id] = poll
        self.bot.save_polls()
        if end_at:
            self._arm_poll_end(poll_id, poll)

    # ------------------------------------------------------------------
    @commands.hybrid_command(name="roll", description="Roll dice, e.g. 2d6, d20, 3d8+2.")
    @app_commands.describe(dice="Dice notation like 2d6, d20, or 3d8+2 (default d6).")
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def roll(self, ctx: commands.Context, dice: str = "d6"):
        """Roll dice in NdM(+K) notation, e.g. `2d6`, `d20`, `3d8+2`."""
        match = _DICE_RE.match(dice.strip())
        if not match:
            await ctx.send(embed=error_embed(f"`{dice}` isn't valid dice notation — try `d20` or `2d6+1`."))
            return
        count = int(match.group("count") or 1)
        sides = int(match.group("sides"))
        bonus = int(match.group("bonus") or 0)
        if not (1 <= count <= 20 and 2 <= sides <= 1000):
            await ctx.send(embed=error_embed("Keep it to 1–20 dice with 2–1000 sides."))
            return
        rolls = [random.randint(1, sides) for _ in range(count)]
        total = sum(rolls) + bonus
        detail = " + ".join(str(r) for r in rolls) + (f" + {bonus}" if bonus else "")
        embed = discord.Embed(
            title=f"🎲 {dice.strip().lower()}",
            description=f"{detail} = **{total}**",
            color=COLOR_FUN,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="coinflip", description="Flip a coin.")
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def coinflip(self, ctx: commands.Context):
        """Flip a coin."""
        result = random.choice(("Heads", "Tails"))
        await ctx.send(embed=discord.Embed(title=f"🪙 {result}!", color=COLOR_FUN))

    @commands.hybrid_command(name="8ball", description="Ask the magic 8-ball a question.")
    @app_commands.describe(question="Your yes/no question.")
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def eightball(self, ctx: commands.Context, *, question: str):
        """Ask the magic 8-ball a yes/no question."""
        embed = discord.Embed(
            title="🎱 The magic 8-ball says…",
            description=f"> {question}\n\n**{random.choice(EIGHT_BALL_ANSWERS)}**",
            color=COLOR_FUN,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="choose", description="Let the bot pick from comma-separated options.")
    @app_commands.describe(options="Comma-separated choices, e.g. burgers, pizza, salad.")
    @commands.cooldown(3, 5, commands.BucketType.user)
    async def choose(self, ctx: commands.Context, *, options: str):
        """Pick one option from a comma-separated list."""
        choices = [c.strip() for c in options.split(",") if c.strip()]
        if len(choices) < 2:
            await ctx.send(embed=error_embed("Give me at least two comma-separated options."))
            return
        embed = discord.Embed(
            title="🤔 I choose…",
            description=f"**{random.choice(choices)}**",
            color=COLOR_FUN,
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Fun(bot))
