"""
BurgBot — referral tracking, ticket role removal, moderation, welcome
messages, reminders, polls, and more, in one Discord bot.

Entrypoint only — see README.md for setup, and:
    config.py   — env-var defaults + per-guild config schema
    storage.py  — atomic JSON persistence
    ui.py       — shared embed/pagination helpers
    core.py     — BurgBot class: state, events, business logic
    cogs/       — commands and interactive UI, grouped by feature

Every command is a *hybrid* command: it works both as a classic prefix
command (default "!") and as a native "/" slash command with Discord's
autocomplete picker. The command tree is synced automatically on startup
(skipped when unchanged, to spare Discord's rate limit); the owner-only
!sync forces a re-sync if Discord's cache seems stale.
"""

import logging
import sys

import discord

from config import COMMAND_PREFIX, DISCORD_TOKEN
from core import BurgBot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("burgbot")

# Privileged intents (enable in the Developer Portal -> Bot -> Privileged
# Gateway Intents):
#   - members:         on_member_join/remove, member/role state, welcome messages
#   - message_content: prefix commands in ordinary messages
# Non-privileged intents used: guilds (baseline), invites (referral tracking).
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True

bot = BurgBot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    help_command=None,  # replaced by the interactive /help in cogs/utility.py
    # Welcome templates and moderation reasons are user-supplied text; never
    # let them ping @everyone or roles.
    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN is not set. Export it (or put it in a .env file next to bot.py) and try again.")
        sys.exit(1)
    bot.run(DISCORD_TOKEN)
