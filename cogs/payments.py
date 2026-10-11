"""
Payment methods: a public /payment display, plus admin-only editing. Methods
are stored per guild (config key "payment_methods"), so each server keeps its
own list and changes take effect immediately.

Given an order total, /payment also quotes the customer's share: half the
order, rounded to a whole dollar (see customer_total).
"""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import TYPE_CHECKING, List, NamedTuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

from ui import error_embed, info_embed, ok_embed

if TYPE_CHECKING:
    from core import BurgBot

MAX_METHODS = 10
MAX_LABEL = 40
MAX_HANDLE = 100
MAX_HOLDER = 60

MAX_ORDER_TOTAL = Decimal("100000")
CENTS = Decimal("0.01")
# Cents at or above this round the customer's total up to the next dollar;
# anything below rounds down. Deliberately not banker's/half-up rounding.
ROUND_UP_FROM = Decimal("0.40")


class Quote(NamedTuple):
    order: Decimal   # what the order cost
    half: Decimal    # exactly half of it, to the cent
    total: int       # half rounded to whole dollars -- what the customer pays


def parse_amount(text: str) -> Optional[Decimal]:
    """Parse an order total like "86.80", "$86.80" or "1,234.50" into cents.
    Returns None if it isn't a usable positive amount."""
    cleaned = text.strip().lstrip("$").replace(",", "").replace(" ", "")
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount > MAX_ORDER_TOTAL:
        return None
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def customer_total(order: Decimal) -> Quote:
    """Half the order, rounded to a whole dollar: 43.40 -> 44, 43.30 -> 43."""
    half = (order / 2).quantize(CENTS, rounding=ROUND_HALF_UP)
    dollars = half.to_integral_value(rounding=ROUND_FLOOR)
    if half - dollars >= ROUND_UP_FROM:
        dollars += 1
    return Quote(order=order, half=half, total=int(dollars))


def payment_embed(guild: discord.Guild, methods: List[dict], quote: Optional[Quote] = None) -> discord.Embed:
    header = f"# YOUR TOTAL IS: ${quote.total:,}\n\n" if quote is not None else ""
    if not methods:
        return info_embed(
            header + "No payment methods are set up yet. An admin can add one with `/setpayment`.",
            title="💳 Payment methods",
        )
    embed = info_embed(
        header + "Send to any of these, then post your confirmation in the ticket.",
        title="💳 Payment methods",
    )
    for method in methods:
        embed.add_field(
            name=method["label"],
            value=f"`{method['handle']}`\nUnder the name **{method['holder']}**",
            inline=False,
        )
    embed.set_footer(text=f"{guild.name} — always double-check the name before sending")
    return embed


class Payments(commands.Cog):
    """Payment method display and configuration."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot

    def _methods(self, guild_id: int) -> List[dict]:
        """This guild's methods as a fresh list of fresh dicts — the stored
        default is shared across guilds, so callers must never mutate it."""
        return [dict(method) for method in self.bot.get_guild_config(guild_id)["payment_methods"]]

    async def _method_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> List[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        return [
            app_commands.Choice(name=method["label"], value=method["label"])
            for method in self._methods(interaction.guild.id)
            if current.lower() in method["label"].lower()
        ][:25]

    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="payment",
        description="Show the payment methods, optionally with the customer's total for an order amount.",
    )
    @app_commands.describe(
        amount="The order total, e.g. 86.80. The customer's total is half of it, rounded to a dollar."
    )
    @commands.guild_only()
    async def payment(self, ctx: commands.Context, amount: Optional[str] = None):
        """Show the accepted payment methods and whose name each is under.
        With an order amount, also quote the customer's total: half the order,
        rounded up from 40 cents and down below that."""
        quote = None
        if amount is not None:
            order = parse_amount(amount)
            if order is None:
                await ctx.send(embed=error_embed(
                    f"`{amount}` isn't an amount I can use — give a number like `86.80`, "
                    f"up to ${MAX_ORDER_TOTAL:,.0f}."
                ), ephemeral=True)
                return
            quote = customer_total(order)
        await ctx.send(embed=payment_embed(ctx.guild, self._methods(ctx.guild.id), quote))

    # ------------------------------------------------------------------
    @commands.hybrid_command(
        name="setpayment", description="Add or update a payment method shown by /payment."
    )
    @app_commands.describe(
        method="Payment method name, e.g. Zelle. Re-using a name updates that entry.",
        handle="The number, tag, or address to send to.",
        holder="The name the account is under.",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def setpayment(self, ctx: commands.Context, method: str, handle: str, holder: str):
        """Add a payment method, or update one by giving an existing method
        name (case-insensitive). Requires Administrator."""
        label, handle, holder = method.strip(), handle.strip(), holder.strip()
        if not (label and handle and holder):
            await ctx.send(embed=error_embed("Method, handle, and holder name can't be blank."), ephemeral=True)
            return
        if len(label) > MAX_LABEL or len(handle) > MAX_HANDLE or len(holder) > MAX_HOLDER:
            await ctx.send(embed=error_embed(
                f"Too long — keep the method under {MAX_LABEL} characters, the handle under "
                f"{MAX_HANDLE}, and the holder name under {MAX_HOLDER}."
            ), ephemeral=True)
            return

        methods = self._methods(ctx.guild.id)
        entry = {"label": label, "handle": handle, "holder": holder}
        existing = next((i for i, m in enumerate(methods) if m["label"].lower() == label.lower()), None)
        if existing is not None:
            # Keep the stored label, so updating a handle can't quietly re-case
            # the method name an admin typed in a hurry.
            entry["label"] = label = methods[existing]["label"]
            methods[existing] = entry
            action = "Updated"
        elif len(methods) >= MAX_METHODS:
            await ctx.send(embed=error_embed(
                f"That's already {MAX_METHODS} payment methods — remove one with `/removepayment` first."
            ), ephemeral=True)
            return
        else:
            methods.append(entry)
            action = "Added"
        self.bot.update_guild_config(ctx.guild.id, payment_methods=methods)

        await self.bot.send_log(
            ctx.guild,
            f"💳 {ctx.author.mention} {action.lower()} the **{label}** payment method "
            f"(`{handle}`, under {holder}).",
        )
        await ctx.send(embed=ok_embed(
            f"{action} **{label}**: `{handle}`, under the name **{holder}**.",
            title="💳 Payment methods updated",
        ))

    @commands.hybrid_command(name="removepayment", description="Remove a payment method from /payment.")
    @app_commands.describe(method="The payment method to remove.")
    @app_commands.autocomplete(method=_method_autocomplete)
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def removepayment(self, ctx: commands.Context, method: str):
        """Remove a payment method by name (case-insensitive). Requires
        Administrator."""
        label = method.strip()
        methods = self._methods(ctx.guild.id)
        existing = next((m for m in methods if m["label"].lower() == label.lower()), None)
        if existing is None:
            known = ", ".join(f"**{m['label']}**" for m in methods) or "*none*"
            await ctx.send(embed=error_embed(
                f"No payment method called **{label}**. Currently set up: {known}."
            ), ephemeral=True)
            return
        label = existing["label"]
        remaining = [m for m in methods if m is not existing]
        self.bot.update_guild_config(ctx.guild.id, payment_methods=remaining)
        await self.bot.send_log(ctx.guild, f"💳 {ctx.author.mention} removed the **{label}** payment method.")
        await ctx.send(embed=ok_embed(f"Removed **{label}** from `/payment`."))


async def setup(bot):
    await bot.add_cog(Payments(bot))
