"""
Central configuration.

Env vars set the *defaults*; per-guild values configured via /setup override
them at runtime (see core.BurgBot.get_guild_config). DISCORD_TOKEN,
BOT_DATA_DIR and COMMAND_PREFIX are process-wide and never per-guild.
"""

import os
from pathlib import Path

# Optional .env support -- keeps `python bot.py` working out of the box for
# people who put their token in a .env file instead of exporting it.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")

# --- Referrals ----------------------------------------------------------
REFERRAL_ROLE_ID: int = int(os.environ.get("REFERRAL_ROLE_ID", "0"))
REFERRALS_NEEDED: int = int(os.environ.get("REFERRALS_NEEDED", "5"))

# --- Anti-abuse ----------------------------------------------------------
# Accounts younger than this are queued for manual staff review instead of
# auto-credited (defends against freshly created alt accounts).
MIN_ACCOUNT_AGE_SECONDS: int = int(os.environ.get("MIN_ACCOUNT_AGE_SECONDS", str(3 * 24 * 3600)))

# A credit is held this long and voided if the member leaves early.
MIN_MEMBER_RETENTION_SECONDS: int = int(os.environ.get("MIN_MEMBER_RETENTION_SECONDS", str(24 * 3600)))

SUSPICIOUS_LOG_CHANNEL_ID: int = int(os.environ.get("SUSPICIOUS_LOG_CHANNEL_ID", "0"))
LOG_CHANNEL_ID: int = int(os.environ.get("LOG_CHANNEL_ID", "0"))

# --- Ticket detection ----------------------------------------------------
CHEF_ROLE_ID: int = int(os.environ.get("CHEF_ROLE_ID", "0"))

# Role pinged when a new ticket channel is created. Falls back to CHEF_ROLE_ID
# when unset. Per-guild override is set via /pingedrole, not the setup wizard.
TICKET_PING_ROLE_ID: int = int(os.environ.get("TICKET_PING_ROLE_ID", "0"))
TICKET_DETECTION_MODE: str = os.environ.get("TICKET_DETECTION_MODE", "and")  # "prefix" | "category" | "and"
TICKET_CHANNEL_PREFIX: str = os.environ.get("TICKET_CHANNEL_PREFIX", "ticket-")
TICKET_CATEGORY_ID: int = int(os.environ.get("TICKET_CATEGORY_ID", "0"))
ROLE_REMOVAL_DELAY_SECONDS: int = int(os.environ.get("ROLE_REMOVAL_DELAY_SECONDS", str(20 * 60)))

# --- Process-wide --------------------------------------------------------
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).parent / "data"))
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")

# --- Per-guild defaults ---------------------------------------------------
# Fields a guild has not configured via /setup fall back to these values,
# so adding a new field here automatically applies to already-configured
# guilds until their admins set it.
DEFAULT_WELCOME_MESSAGE = "Welcome to **{server}**, {mention}! You are member **#{count}**. 🎉"
DEFAULT_GOODBYE_MESSAGE = "**{name}** has left {server}. We are now **{count}** members."

DEFAULT_GUILD_CONFIG: dict = {
    "referral_role_id": REFERRAL_ROLE_ID,
    "referrals_needed": REFERRALS_NEEDED,
    "chef_role_id": CHEF_ROLE_ID,
    "ticket_detection_mode": TICKET_DETECTION_MODE,
    "ticket_channel_prefix": TICKET_CHANNEL_PREFIX,
    "ticket_category_id": TICKET_CATEGORY_ID,
    "role_removal_delay_seconds": ROLE_REMOVAL_DELAY_SECONDS,
    "min_account_age_seconds": MIN_ACCOUNT_AGE_SECONDS,
    "min_member_retention_seconds": MIN_MEMBER_RETENTION_SECONDS,
    "suspicious_log_channel_id": SUSPICIOUS_LOG_CHANNEL_ID,
    "log_channel_id": LOG_CHANNEL_ID,
    "ticket_ping_role_id": TICKET_PING_ROLE_ID,
    "welcome_channel_id": 0,
    "welcome_message": DEFAULT_WELCOME_MESSAGE,
    "goodbye_channel_id": 0,
    "goodbye_message": DEFAULT_GOODBYE_MESSAGE,
}

# --- Setup wizard preset choices ------------------------------------------
REFERRALS_NEEDED_CHOICES = [1, 3, 5, 10, 15, 20, 25, 50]

DELAY_CHOICES = [
    ("5 minutes", 300), ("10 minutes", 600), ("20 minutes", 1200),
    ("30 minutes", 1800), ("1 hour", 3600), ("2 hours", 7200),
]
ACCOUNT_AGE_CHOICES = [
    ("Disabled (no minimum)", 0), ("1 day", 86400), ("3 days", 259200),
    ("7 days", 604800), ("14 days", 1209600), ("30 days", 2592000),
]
RETENTION_CHOICES = [
    ("Disabled (credit immediately)", 0), ("1 hour", 3600), ("6 hours", 21600),
    ("12 hours", 43200), ("24 hours", 86400), ("48 hours", 172800), ("72 hours", 259200),
]

# Extensions loaded on startup. Order matters only for /help category order.
EXTENSIONS = [
    "cogs.referrals",
    "cogs.tickets",
    "cogs.welcome",
    "cogs.moderation",
    "cogs.utility",
    "cogs.fun",
    "cogs.admin",
]
