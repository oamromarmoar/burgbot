"""
JSON persistence: one file per dataset under BOT_DATA_DIR, written atomically
(tmp file + rename) so a crash mid-write can never corrupt existing data.
"""

import json
import logging
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger("burgbot.storage")

REFERRAL_PROGRESS_FILE = DATA_DIR / "referral_progress.json"
TOTAL_REFERRALS_FILE = DATA_DIR / "total_referrals.json"
PENDING_REMOVALS_FILE = DATA_DIR / "pending_removals.json"
TICKET_OPENERS_FILE = DATA_DIR / "ticket_openers.json"
CREDITED_MEMBERS_FILE = DATA_DIR / "credited_members.json"
PENDING_CREDITS_FILE = DATA_DIR / "pending_credits.json"
SUSPICIOUS_JOINS_FILE = DATA_DIR / "suspicious_joins.json"
GUILD_CONFIG_FILE = DATA_DIR / "guild_config.json"
SYNCED_COMMANDS_FILE = DATA_DIR / "synced_commands.json"
WARNINGS_FILE = DATA_DIR / "warnings.json"
REMINDERS_FILE = DATA_DIR / "reminders.json"
POLLS_FILE = DATA_DIR / "polls.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load %s (%s); starting with default value.", path, exc)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)  # atomic on POSIX and Windows


def load_per_guild_counts(path: Path) -> dict:
    """Load a {guild_id: {user_id: count}} file, wrapping data written by the
    old single-guild schema ({user_id: count}) under "_legacy" so it can be
    migrated once the bot knows which guild it belongs to."""
    data = load_json(path, {})
    if data and any(isinstance(v, int) for v in data.values()):
        log.info("Detected legacy flat counts in %s; queued for migration.", path.name)
        return {"_legacy": data}
    return data
