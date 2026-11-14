"""
Order tracker: one sheet per *project*, each tied to a Discord category.
Every ticket channel in that category is a row — ticket number (taken from
the channel name), status, order #, store, what the customer paid, and
profit. Values are filled in from inside the ticket (/ordernum, /store,
/rfpaid, /prof, and one command per status); /rforders renders the sheet as
a colour-coded chart image with the project's total profit at the bottom.

Changing a ticket's status or store renames its channel to show both, e.g.
"🔵BOUGHT-22-WALMART" (Discord itself lowercases text-channel names). /complete
deletes the ticket 12 hours later; moving the status back cancels that.

Statuses only move forward one step at a time — Opened → Bought →
Delivered → Pending → Partial → Complete — except that Pending may jump
straight to Complete. Moving backwards is always allowed, to fix mistakes.
"""

import asyncio
import io
import logging
import re
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import TYPE_CHECKING, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

from ui import error_embed, info_embed, ok_embed, warn_embed

if TYPE_CHECKING:
    from core import BurgBot

log = logging.getLogger(__name__)

STATUSES = ["Opened", "Bought", "Delivered", "Pending", "Partial", "Complete"]
# The only forward move allowed to skip a step.
ALLOWED_SKIPS = {("Pending", "Complete")}

# status -> (fill RGB, pill text RGB, emoji for text replies)
STATUS_STYLE = {
    "Opened": ((148, 155, 164), (20, 21, 24), "⚪"),
    "Bought": ((88, 101, 242), (255, 255, 255), "🔵"),
    "Delivered": ((163, 113, 247), (255, 255, 255), "🟣"),
    "Pending": ((240, 178, 50), (20, 21, 24), "🟡"),
    "Partial": ((242, 128, 54), (20, 21, 24), "🟠"),
    "Complete": ((35, 165, 90), (255, 255, 255), "🟢"),
}

MAX_PROJECT_NAME = 40
MAX_ORDER_NUM = 40
MAX_STORE = 40
MAX_MONEY = Decimal("1000000")
CENTS = Decimal("0.01")

# Everything but the /rforders chart is a private reply that clears itself.
REPLY_SECONDS = 10
CONFIRM_SECONDS = 60
# /complete deletes the ticket after this long, unless the status is moved back.
COMPLETE_DELETE_SECONDS = 12 * 60 * 60

ROWS_PER_PAGE = 40
MAX_PAGES = 10  # Discord's per-message embed/attachment limit

_DIGITS_RE = re.compile(r"\d+")
# The name this cog gives tickets: "<emoji>STATUS-<number>[-STORE]".
_RENAMED_RE = re.compile(r"^\W*[A-Za-z]+-(\d+)(?:-|$)")


# ----------------------------------------------------------------------
# Pure helpers
# ----------------------------------------------------------------------
def ticket_number(channel_name: str) -> str:
    """The ticket's number from its channel name: "ticket-0042" -> "0042",
    "🔵bought-22-7-eleven" -> "22". Falls back to the last digits in the
    name, then to the whole name when it has no digits."""
    match = _RENAMED_RE.match(channel_name)
    if match:
        return match.group(1)
    numbers = _DIGITS_RE.findall(channel_name)
    return numbers[-1] if numbers else channel_name


def channel_name_for(order: dict) -> str:
    """The channel name for a ticket: "🔵BOUGHT-22", plus "-WALMART" once a
    store is set. Spaces become hyphens, as Discord would do anyway."""
    name = f"{STATUS_STYLE[order['status']][2]}{order['status'].upper()}-{order['ticket']}"
    if order.get("store"):
        name += "-" + "-".join(order["store"].upper().split())
    return name[:100]


def _ticket_sort_key(order: dict):
    ticket = order["ticket"]
    return (0, int(ticket), ticket) if ticket.isdigit() else (1, 0, ticket.lower())


def transition_error(current: str, new: str) -> Optional[str]:
    """Why `current` can't move to `new`, or None if it can."""
    if current == new:
        return f"This ticket is already **{new}**."
    old_index, new_index = STATUSES.index(current), STATUSES.index(new)
    if new_index < old_index or new_index == old_index + 1 or (current, new) in ALLOWED_SKIPS:
        return None
    expected = STATUSES[old_index + 1]
    extra = " (or straight to **Complete**)" if current == "Pending" else ""
    return f"Can't go from **{current}** to **{new}** — the next step is **{expected}**{extra}."


def parse_money(text: str, *, allow_negative: bool = False) -> Optional[Decimal]:
    """Parse "12", "$12.50", "1,234.5" (and "-5" if allowed) to cents."""
    cleaned = text.strip().replace(",", "").replace(" ", "")
    negative = cleaned.startswith("-")
    cleaned = cleaned.lstrip("-").lstrip("$")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount < 0 or amount > MAX_MONEY:
        return None
    if negative:
        if not allow_negative:
            return None
        amount = -amount
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


# Pillow's built-in font has no em dash, so blank cells use a plain hyphen.
BLANK = "-"


def format_money(value: Optional[str]) -> str:
    if value is None:
        return BLANK
    amount = Decimal(value)
    return f"-${-amount:,.2f}" if amount < 0 else f"${amount:,.2f}"


def total_profit(orders: List[dict]) -> Decimal:
    return sum((Decimal(o["profit"]) for o in orders if o.get("profit") is not None), Decimal("0"))


# ----------------------------------------------------------------------
# Chart rendering
# ----------------------------------------------------------------------
BG = (30, 31, 34)
ROW_A = (43, 45, 49)
ROW_B = (49, 51, 56)
HEADER_BG = (24, 25, 28)
TEXT = (242, 243, 245)
MUTED = (148, 155, 164)
PROFIT_UP = (35, 165, 90)
PROFIT_DOWN = (237, 66, 69)

PAD = 24
ROW_H = 40
CELL_PAD = 18
COLUMNS = ["Ticket #", "Status", "Order #", "Store", "Paid", "Profit"]


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def render_chart(
    project_name: str, orders: List[dict], *, page: int, pages: int, total: Optional[Decimal]
) -> io.BytesIO:
    """Draw one page of the orders sheet as a PNG. `total` is drawn bottom
    left when given (the last page)."""
    font = ImageFont.load_default(size=17)
    header_font = ImageFont.load_default(size=15)
    title_font = ImageFont.load_default(size=24)
    total_font = ImageFont.load_default(size=26)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))

    rows = [
        [
            o["ticket"],
            o["status"],
            o.get("order_num") or BLANK,
            o.get("store") or BLANK,
            format_money(o.get("paid")),
            format_money(o.get("profit")),
        ]
        for o in orders
    ]
    max_cell = 220  # keeps long store names / order numbers from blowing out the width
    widths = []
    for i, heading in enumerate(COLUMNS):
        cells = [measure.textlength(r[i], font=font) for r in rows]
        widths.append(int(min(max([measure.textlength(heading, font=header_font), *cells]), max_cell)) + 2 * CELL_PAD)
    widths[1] = max(widths[1], int(max(measure.textlength(s, font=font) for s in STATUSES)) + 2 * CELL_PAD + 16)

    table_w = sum(widths)
    title_h = 56
    footer_h = 64 if total is not None else 16
    width = max(table_w, 420) + 2 * PAD
    height = PAD + title_h + ROW_H * (max(len(rows), 1) + 1) + footer_h + PAD
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    # Title
    draw.text((PAD, PAD), project_name, font=title_font, fill=TEXT)
    if pages > 1:
        label = f"Page {page}/{pages}"
        draw.text((width - PAD - draw.textlength(label, font=header_font), PAD + 6), label, font=header_font, fill=MUTED)

    # Header row
    y = PAD + title_h
    draw.rectangle([PAD, y, PAD + table_w, y + ROW_H], fill=HEADER_BG)
    x = PAD
    for heading, w in zip(COLUMNS, widths):
        draw.text((x + CELL_PAD, y + ROW_H // 2), heading.upper(), font=header_font, fill=MUTED, anchor="lm")
        x += w

    # Body rows
    for index, (order, row) in enumerate(zip(orders, rows)):
        y += ROW_H
        draw.rectangle([PAD, y, PAD + table_w, y + ROW_H], fill=ROW_A if index % 2 == 0 else ROW_B)
        x = PAD
        for col, (cell, w) in enumerate(zip(row, widths)):
            mid = y + ROW_H // 2
            if col == 1:
                fill, text_fill, _ = STATUS_STYLE[order["status"]]
                pill_w = draw.textlength(cell, font=font) + 24
                draw.rounded_rectangle([x + CELL_PAD - 8, mid - 13, x + CELL_PAD - 8 + pill_w, mid + 13],
                                       radius=13, fill=fill)
                draw.text((x + CELL_PAD + 4, mid), cell, font=font, fill=text_fill, anchor="lm")
            else:
                color = TEXT
                if col == 5 and order.get("profit") is not None:
                    color = PROFIT_DOWN if Decimal(order["profit"]) < 0 else PROFIT_UP
                elif cell == BLANK:
                    color = MUTED
                draw.text((x + CELL_PAD, mid), _truncate(draw, cell, font, w - 2 * CELL_PAD),
                          font=font, fill=color, anchor="lm")
            x += w

    if not rows:
        y += ROW_H
        draw.text((PAD + CELL_PAD, y + ROW_H // 2), "No tickets yet.", font=font, fill=MUTED, anchor="lm")

    # Total profit, bottom left, on its own
    if total is not None:
        y += ROW_H + 24
        draw.text((PAD, y), "Total profit: ", font=total_font, fill=TEXT)
        offset = draw.textlength("Total profit: ", font=total_font)
        draw.text((PAD + offset, y), format_money(str(total)), font=total_font,
                  fill=PROFIT_DOWN if total < 0 else PROFIT_UP)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
class ConfirmDeleteView(discord.ui.View):
    def __init__(self, cog: "Orders", author_id: int, guild_id: int, key: str):
        super().__init__(timeout=60)
        self.cog, self.author_id, self.guild_id, self.key = cog, author_id, guild_id, key
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Only the person who ran the command can confirm.")
        return False

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass

    @discord.ui.button(label="Delete project", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        project = self.cog._projects(self.guild_id).pop(self.key, None)
        self.cog.bot.save_rf_projects()
        self.stop()
        if project is None:
            await interaction.response.edit_message(
                embed=error_embed("That project is already gone."), view=None, delete_after=REPLY_SECONDS
            )
            return
        await interaction.response.edit_message(
            embed=ok_embed(f"Deleted **{project['name']}** and its {len(project['orders'])} order row(s)."),
            view=None, delete_after=REPLY_SECONDS,
        )
        await self.cog.bot.send_log(
            interaction.guild, f"📦 {interaction.user.mention} deleted the **{project['name']}** order project."
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=info_embed("Kept the project."), view=None, delete_after=REPLY_SECONDS
        )


class UndoCompleteView(discord.ui.View):
    """Undo button on /complete's reply, shown while the ticket waits to be
    deleted. Puts the status back and cancels the deletion. (The button
    doesn't survive a restart; moving the status back does the same.)"""

    def __init__(self, cog: "Orders", author_id: int, channel: discord.TextChannel, previous: str):
        super().__init__(timeout=COMPLETE_DELETE_SECONDS)
        self.cog, self.author_id, self.channel, self.previous = cog, author_id, channel, previous

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        order = self.cog._order_for_channel(self.channel.guild.id, self.channel.id)
        if order is None or order["status"] != "Complete":
            await interaction.response.edit_message(
                embed=info_embed("Nothing to undo — this ticket isn't Complete any more."),
                view=None, delete_after=REPLY_SECONDS,
            )
            return
        order["status"] = self.previous
        self.cog._cancel_delete(order, self.channel.id)
        self.cog.bot.save_rf_projects()
        self.cog._schedule_rename(self.channel)
        await interaction.response.edit_message(
            embed=info_embed(f"Undone — the ticket stays open and is back to **{self.previous}**."),
            view=None, delete_after=REPLY_SECONDS,
        )


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class Orders(commands.Cog):
    """Per-project order tracker: status, order #, store, paid, and profit per ticket."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot
        self._delete_tasks: dict = {}   # channel id -> pending /complete deletion
        self._rename_tasks: dict = {}   # channel id -> running rename worker
        self._rename_wanted: set = set()  # channel ids whose name is out of date

    async def cog_load(self):
        # Re-arm /complete deletions that were waiting when the bot stopped.
        for guild_id, projects in self.bot.rf_projects.items():
            for project in projects.values():
                for channel_id, order in project["orders"].items():
                    if order["status"] == "Complete" and order.get("delete_at"):
                        self._arm_delete(int(guild_id), int(channel_id), order["delete_at"])

    async def cog_unload(self):
        for task in [*self._delete_tasks.values(), *self._rename_tasks.values()]:
            task.cancel()

    # --- channel renames ----------------------------------------------
    def _schedule_rename(self, channel: discord.TextChannel):
        """Bring the channel's name in line with its order row. Runs in the
        background: Discord allows only 2 renames per channel per 10 minutes,
        and discord.py waits out that limit rather than failing. Requests made
        while a rename is in flight collapse into one more rename."""
        self._rename_wanted.add(channel.id)
        if channel.id not in self._rename_tasks:
            self._rename_tasks[channel.id] = asyncio.create_task(
                self._rename_worker(channel.guild.id, channel.id)
            )

    async def _rename_worker(self, guild_id: int, channel_id: int):
        try:
            while channel_id in self._rename_wanted:
                self._rename_wanted.discard(channel_id)
                channel = self.bot.get_channel(channel_id)
                order = self._order_for_channel(guild_id, channel_id)
                if channel is None or order is None:
                    return
                name = channel_name_for(order)
                if channel.name.lower() == name.lower():
                    continue
                try:
                    await channel.edit(name=name, reason="Order status/store changed")
                except discord.NotFound:
                    return
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Couldn't rename ticket channel %s: %s", channel_id, exc)
                    await self.bot.send_log(
                        channel.guild, f"⚠️ Couldn't rename {channel.mention} to `{name}` ({exc}). "
                                       "I need **Manage Channels** in that category."
                    )
                    return
        finally:
            self._rename_tasks.pop(channel_id, None)
            self._rename_wanted.discard(channel_id)

    # --- delayed /complete deletion -----------------------------------
    def _arm_delete(self, guild_id: int, channel_id: int, delete_at: float):
        old = self._delete_tasks.pop(channel_id, None)
        if old is not None:
            old.cancel()
        self._delete_tasks[channel_id] = asyncio.create_task(
            self._delete_worker(guild_id, channel_id, delete_at)
        )

    def _cancel_delete(self, order: dict, channel_id: int):
        order.pop("delete_at", None)
        task = self._delete_tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()

    async def _delete_worker(self, guild_id: int, channel_id: int, delete_at: float):
        try:
            await self.bot.wait_until_ready()
            await asyncio.sleep(max(0.0, delete_at - time.time()))
            order = self._order_for_channel(guild_id, channel_id)
            # Moving the status back (or Undo) clears delete_at.
            if order is None or order["status"] != "Complete" or not order.get("delete_at"):
                return
            order.pop("delete_at", None)
            self.bot.save_rf_projects()
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                return
            try:
                await channel.delete(reason="Order complete (12h after /complete)")
            except discord.NotFound:
                pass
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Couldn't delete completed ticket %s: %s", channel_id, exc)
                await self.bot.send_log(
                    channel.guild, f"⚠️ {channel.mention} is Complete, but I couldn't delete it ({exc}). "
                                   "I need **Manage Channels** in that category."
                )
        finally:
            if self._delete_tasks.get(channel_id) is asyncio.current_task():
                del self._delete_tasks[channel_id]

    async def _reply(self, ctx: commands.Context, embed: discord.Embed, *, view: Optional[discord.ui.View] = None,
                     seconds: float = REPLY_SECONDS) -> discord.Message:
        """A reply only the invoker sees, deleted after `seconds`. Prefix
        commands can't be private, so their command message is deleted too
        and the reply is on screen only briefly."""
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
        return await ctx.send(embed=embed, view=view, ephemeral=True, delete_after=seconds)

    # --- state helpers -------------------------------------------------
    def _projects(self, guild_id: int) -> dict:
        return self.bot.rf_projects.setdefault(str(guild_id), {})

    def _order_for_channel(self, guild_id: int, channel_id: int) -> Optional[dict]:
        for project in self._projects(guild_id).values():
            order = project["orders"].get(str(channel_id))
            if order is not None:
                return order
        return None

    def _project_for_category(self, guild_id: int, category_id: Optional[int]) -> Optional[dict]:
        if category_id is None:
            return None
        return next((p for p in self._projects(guild_id).values() if p["category_id"] == category_id), None)

    def _ensure_order(self, project: dict, channel: discord.TextChannel) -> dict:
        """The channel's row, created as Opened if new. Keeps the ticket
        number in step with the channel name in case it was renamed."""
        order = project["orders"].get(str(channel.id))
        if order is None:
            order = {
                "ticket": ticket_number(channel.name),
                "status": "Opened",
                "order_num": None,
                "store": None,
                "paid": None,
                "profit": None,
                "created_at": time.time(),
            }
            project["orders"][str(channel.id)] = order
        else:
            order["ticket"] = ticket_number(channel.name)
        return order

    async def _ticket_order(self, ctx: commands.Context) -> Optional[Tuple[dict, dict]]:
        """(project, order row) for the ticket this command ran in, or None
        after telling the user why not."""
        channel = ctx.channel
        project = (
            self._project_for_category(ctx.guild.id, channel.category_id)
            if isinstance(channel, discord.TextChannel) else None
        )
        if project is None:
            await self._reply(ctx, error_embed(
                "This channel isn't in a project's category. Run this inside a ticket, or create a project "
                "for this category with `/rfproject create`."
            ))
            return None
        return project, self._ensure_order(project, channel)

    def _resolve_project(self, ctx: commands.Context, name: Optional[str]) -> Tuple[Optional[dict], Optional[str]]:
        """Pick a project by name, else the one for the current channel's
        category, else the only one. Returns (project, error)."""
        projects = self._projects(ctx.guild.id)
        if name:
            project = projects.get(name.strip().lower())
            if project is None:
                return None, f"No project called **{name}**. {self._known(projects)}"
            return project, None
        project = self._project_for_category(ctx.guild.id, getattr(ctx.channel, "category_id", None))
        if project:
            return project, None
        if len(projects) == 1:
            return next(iter(projects.values())), None
        if not projects:
            return None, "No projects yet — create one with `/rfproject create`."
        return None, f"Which project? {self._known(projects)}"

    @staticmethod
    def _known(projects: dict) -> str:
        names = ", ".join(f"**{p['name']}**" for p in projects.values())
        return f"Projects: {names}." if names else "There are no projects yet."

    async def _project_autocomplete(self, interaction: discord.Interaction, current: str):
        if interaction.guild is None:
            return []
        return [
            app_commands.Choice(name=p["name"], value=p["name"])
            for p in self._projects(interaction.guild.id).values()
            if current.lower() in p["name"].lower()
        ][:25]

    # --- auto-add new tickets -----------------------------------------
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not isinstance(channel, discord.TextChannel):
            return
        project = self._project_for_category(channel.guild.id, channel.category_id)
        if project is None:
            return
        self._ensure_order(project, channel)
        self.bot.save_rf_projects()

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Drop a deleted ticket's row, unless it was Complete -- those stay
        as the record of the order (and keep counting toward total profit)."""
        changed = False
        self._rename_wanted.discard(channel.id)
        for project in self._projects(channel.guild.id).values():
            order = project["orders"].get(str(channel.id))
            if order is None:
                continue
            if order.get("delete_at"):
                self._cancel_delete(order, channel.id)
                changed = True
            if order["status"] != "Complete":
                del project["orders"][str(channel.id)]
                changed = True
        if changed:
            self.bot.save_rf_projects()

    # --- projects -----------------------------------------------------
    @commands.hybrid_group(
        name="rfproject", description="Manage order-tracking projects (one per ticket category).",
        invoke_without_command=True,
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rfproject(self, ctx: commands.Context):
        """Order-tracking projects. Prefix-only: with no subcommand, lists them."""
        await self.rfproject_list(ctx)

    @rfproject.command(name="create", description="Create a project that tracks every ticket in a category.")
    @app_commands.describe(name="Project name, e.g. Uber Eats.", category="The category its tickets are opened in.")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rfproject_create(self, ctx: commands.Context, category: discord.CategoryChannel, *, name: str):
        """Create a project tied to one category. Ticket channels already in
        it are added as Opened; new ones are added as they're created."""
        name = name.strip()
        if not name or len(name) > MAX_PROJECT_NAME:
            await self._reply(ctx, error_embed(f"Give a name up to {MAX_PROJECT_NAME} characters."))
            return
        projects = self._projects(ctx.guild.id)
        if name.lower() in projects:
            await self._reply(ctx, error_embed(f"There's already a project called **{name}**."))
            return
        clash = self._project_for_category(ctx.guild.id, category.id)
        if clash:
            await self._reply(ctx, error_embed(
                f"**{category.name}** is already tracked by **{clash['name']}**."
            ))
            return

        project = {"name": name, "category_id": category.id, "created_at": time.time(), "orders": {}}
        for channel in category.text_channels:
            self._ensure_order(project, channel)
        projects[name.lower()] = project
        self.bot.save_rf_projects()

        await self.bot.send_log(
            ctx.guild, f"📦 {ctx.author.mention} created the **{name}** order project for **{category.name}**."
        )
        await self._reply(ctx, ok_embed(
            f"Created **{name}**, tracking tickets in **{category.name}**. "
            f"Added {len(project['orders'])} existing ticket(s) as Opened.\n"
            f"View it with `/rforders`.",
            title="📦 Project created",
        ))

    @rfproject.command(name="delete", description="Delete a project and all of its order rows.")
    @app_commands.describe(name="The project to delete.")
    @app_commands.autocomplete(name=_project_autocomplete)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rfproject_delete(self, ctx: commands.Context, *, name: str):
        """Delete a project, after a confirmation button."""
        key = name.strip().lower()
        projects = self._projects(ctx.guild.id)
        project = projects.get(key)
        if project is None:
            await self._reply(ctx, error_embed(f"No project called **{name}**. {self._known(projects)}"))
            return
        view = ConfirmDeleteView(self, ctx.author.id, ctx.guild.id, key)
        view.message = await self._reply(ctx, warn_embed(
            f"Delete **{project['name']}** and all {len(project['orders'])} of its order rows? This can't be undone."
        ), view=view, seconds=CONFIRM_SECONDS)

    @rfproject.command(name="list", description="List order-tracking projects and their categories.")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rfproject_list(self, ctx: commands.Context):
        """List every project, its category, ticket count and total profit."""
        projects = self._projects(ctx.guild.id)
        if not projects:
            await self._reply(ctx, info_embed("No projects yet — create one with `/rfproject create`."))
            return
        lines = []
        for p in projects.values():
            category = ctx.guild.get_channel(p["category_id"])
            where = f"**{category.name}**" if category else f"`{p['category_id']}` (category not found)"
            orders = list(p["orders"].values())
            lines.append(
                f"**{p['name']}** — {where} · {len(orders)} ticket(s) · "
                f"profit {format_money(str(total_profit(orders)))}"
            )
        await self._reply(ctx, info_embed("\n".join(lines), title="📦 Order projects"))

    # --- the chart ----------------------------------------------------
    @commands.hybrid_command(name="rforders", description="Show a project's order chart and total profit.")
    @app_commands.describe(project="Which project. Defaults to this ticket's project, or the only one.")
    @app_commands.autocomplete(project=_project_autocomplete)
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rforders(self, ctx: commands.Context, *, project: Optional[str] = None):
        """Render the project's sheet: one row per ticket, colour-coded by
        status, with total profit at the bottom."""
        found, problem = self._resolve_project(ctx, project)
        if problem:
            await self._reply(ctx, error_embed(problem))
            return
        await ctx.defer(ephemeral=True)

        # Refresh ticket numbers from live channel names (renames).
        for channel_id, order in found["orders"].items():
            channel = ctx.guild.get_channel(int(channel_id))
            if channel is not None:
                order["ticket"] = ticket_number(channel.name)
        orders = sorted(found["orders"].values(), key=_ticket_sort_key)
        total = total_profit(orders)

        chunks = [orders[i:i + ROWS_PER_PAGE] for i in range(0, len(orders), ROWS_PER_PAGE)] or [[]]
        dropped = 0
        if len(chunks) > MAX_PAGES:
            # Show the newest tickets; the total still counts every row.
            dropped = sum(len(c) for c in chunks[:-MAX_PAGES])
            chunks = chunks[-MAX_PAGES:]

        def build():
            return [
                render_chart(found["name"], chunk, page=i + 1, pages=len(chunks),
                             total=total if i == len(chunks) - 1 else None)
                for i, chunk in enumerate(chunks)
            ]

        images = await asyncio.to_thread(build)

        counts = {s: 0 for s in STATUSES}
        for o in orders:
            counts[o["status"]] += 1
        summary = "  ".join(f"{STATUS_STYLE[s][2]} {s} **{n}**" for s, n in counts.items() if n)
        if dropped:
            summary += f"\n*{dropped} older ticket(s) not shown; they're still counted in the total.*"

        files, embeds = [], []
        for i, buffer in enumerate(images):
            filename = f"orders-{i + 1}.png"
            files.append(discord.File(buffer, filename=filename))
            embed = discord.Embed(color=discord.Color.blurple())
            if i == 0:
                embed.title = f"📦 {found['name']} — orders"
                embed.description = summary or "No tickets yet."
            embed.set_image(url=f"attachment://{filename}")
            embeds.append(embed)
        await ctx.send(embeds=embeds, files=files, ephemeral=True)

    # --- in-ticket: status --------------------------------------------
    async def _set_status(
        self, ctx: commands.Context, status: str, *, note: str = "",
        view=None, seconds: Optional[float] = REPLY_SECONDS,
    ) -> Optional[Tuple[dict, str, discord.Message]]:
        """Move this ticket to `status` and queue the channel rename. `view`,
        if given, is called with the previous status to build the reply's
        view. Returns (order row, previous status, reply) on success, None
        after replying with why not."""
        found = await self._ticket_order(ctx)
        if found is None:
            return None
        project, order = found
        problem = transition_error(order["status"], status)
        if problem:
            self.bot.save_rf_projects()  # a brand-new row may have been created
            await self._reply(ctx, error_embed(problem))
            return None
        previous, order["status"] = order["status"], status
        if previous == "Complete":
            self._cancel_delete(order, ctx.channel.id)
            note += "\nThe scheduled deletion is cancelled."
        self.bot.save_rf_projects()
        self._schedule_rename(ctx.channel)
        fill, _, emoji = STATUS_STYLE[status]
        reply = await self._reply(ctx, discord.Embed(
            description=f"{emoji} Ticket **{order['ticket']}** ({project['name']}): {previous} → **{status}**{note}",
            color=discord.Color.from_rgb(*fill),
        ), view=view(previous) if view else None, seconds=seconds)
        return order, previous, reply

    _STATUS_HELP = "Set this ticket's order status to {}."

    @commands.hybrid_command(name="opened", description=_STATUS_HELP.format("Opened"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def opened(self, ctx: commands.Context):
        """Set this ticket's status to Opened (moving back is always allowed)."""
        await self._set_status(ctx, "Opened")

    @commands.hybrid_command(name="bought", description=_STATUS_HELP.format("Bought"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def bought(self, ctx: commands.Context):
        """Set this ticket's status to Bought (from Opened)."""
        await self._set_status(ctx, "Bought")

    @commands.hybrid_command(name="delivered", description=_STATUS_HELP.format("Delivered"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def delivered(self, ctx: commands.Context):
        """Set this ticket's status to Delivered (from Bought)."""
        await self._set_status(ctx, "Delivered")

    @commands.hybrid_command(name="pending", description=_STATUS_HELP.format("Pending"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def pending(self, ctx: commands.Context):
        """Set this ticket's status to Pending (from Delivered)."""
        await self._set_status(ctx, "Pending")

    @commands.hybrid_command(name="partial", description=_STATUS_HELP.format("Partial"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def partial(self, ctx: commands.Context):
        """Set this ticket's status to Partial (from Pending)."""
        await self._set_status(ctx, "Partial")

    @commands.hybrid_command(name="complete", description=_STATUS_HELP.format("Complete"))
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def complete(self, ctx: commands.Context):
        """Set this ticket's status to Complete (from Partial, or straight
        from Pending), then delete the ticket channel 12 hours later. Undo, or
        moving the status back (e.g. /partial), cancels the deletion. The row
        stays on the chart."""
        delete_at = time.time() + COMPLETE_DELETE_SECONDS
        done = await self._set_status(
            ctx, "Complete", seconds=None,  # the channel deletion clears it
            view=lambda previous: UndoCompleteView(self, ctx.author.id, ctx.channel, previous),
            note=f"\nDeleting this ticket <t:{int(delete_at)}:R>. Undo, or move the status back "
                 "(e.g. `/partial`), to keep it.",
        )
        if done is None:
            return
        order, _, _ = done
        order["delete_at"] = delete_at
        self.bot.save_rf_projects()
        self._arm_delete(ctx.guild.id, ctx.channel.id, delete_at)

    @commands.hybrid_command(name="status", description="Check the status of your order in this ticket.")
    @commands.guild_only()
    async def status(self, ctx: commands.Context):
        """Anyone (customers included) can check this ticket's order status.
        Shows the status, store and order # -- never what was paid or profit."""
        found = await self._ticket_order(ctx)
        if found is None:
            return
        project, order = found
        self.bot.save_rf_projects()  # a brand-new row may have been created
        current = order["status"]
        fill, _, emoji = STATUS_STYLE[current]
        steps = " → ".join(f"**{s}**" if s == current else s for s in STATUSES)
        lines = [f"{emoji} Your order is **{current}**.", steps]
        if order.get("store"):
            lines.append(f"**Store:** {order['store']}")
        if order.get("order_num"):
            lines.append(f"**Order #:** {order['order_num']}")
        if current == "Complete" and order.get("delete_at"):
            lines.append(f"This ticket will close <t:{int(order['delete_at'])}:R>.")
        embed = discord.Embed(
            title=f"📦 Ticket {order['ticket']} — {project['name']}",
            description="\n".join(lines),
            color=discord.Color.from_rgb(*fill),
        )
        await self._reply(ctx, embed, seconds=60)

    # --- in-ticket: fields --------------------------------------------
    async def _set_field(self, ctx: commands.Context, field: str, label: str, value, shown: str):
        found = await self._ticket_order(ctx)
        if found is None:
            return
        project, order = found
        order[field] = value
        self.bot.save_rf_projects()
        if field == "store":
            self._schedule_rename(ctx.channel)
        await self._reply(ctx, ok_embed(
            f"Ticket **{order['ticket']}** ({project['name']}): {label} set to **{shown}**."
        ))

    @commands.hybrid_command(name="ordernum", description="Set this ticket's order number.")
    @app_commands.describe(number="The order number, e.g. 1111.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def ordernum(self, ctx: commands.Context, *, number: str):
        """Set the order # column for this ticket."""
        number = number.strip()
        if not number or len(number) > MAX_ORDER_NUM:
            await self._reply(ctx, error_embed(f"Give an order number up to {MAX_ORDER_NUM} characters."))
            return
        await self._set_field(ctx, "order_num", "Order #", number, number)

    @commands.hybrid_command(name="store", description="Set which store this ticket's order is from.")
    @app_commands.describe(name="The store, e.g. Walmart.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def store(self, ctx: commands.Context, *, name: str):
        """Set the Store column for this ticket."""
        name = name.strip()
        if not name or len(name) > MAX_STORE:
            await self._reply(ctx, error_embed(f"Give a store name up to {MAX_STORE} characters."))
            return
        await self._set_field(ctx, "store", "Store", name, name)

    @commands.hybrid_command(name="rfpaid", description="Set how much the customer paid for this ticket.")
    @app_commands.describe(amount="What the customer paid, e.g. 45 or 45.50.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def rfpaid(self, ctx: commands.Context, amount: str):
        """Set the Paid column for this ticket."""
        value = parse_money(amount)
        if value is None:
            await self._reply(ctx, error_embed(f"`{amount}` isn't an amount I can use — try `45` or `45.50`."))
            return
        await self._set_field(ctx, "paid", "Paid", str(value), format_money(str(value)))

    @commands.hybrid_command(name="prof", description="Set the profit made on this ticket.")
    @app_commands.describe(amount="The profit, e.g. 20 or 20.50. Negative for a loss, e.g. -5.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def prof(self, ctx: commands.Context, amount: str):
        """Set the Profit column for this ticket (negative for a loss)."""
        value = parse_money(amount, allow_negative=True)
        if value is None:
            await self._reply(ctx, error_embed(f"`{amount}` isn't an amount I can use — try `20`, `20.50` or `-5`."))
            return
        await self._set_field(ctx, "profit", "Profit", str(value), format_money(str(value)))


async def setup(bot):
    await bot.add_cog(Orders(bot))
