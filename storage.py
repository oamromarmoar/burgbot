"""
JSON persistence: one file per dataset under BOT_DATA_DIR, written atomically
(tmp file + rename) so a crash mid-write can never corrupt existing data.
"""

import json
import logging
from pathlib import Path

from config import DATA_DIR

log = logging.getLogger("burgbot.storage")

PENDING_REMOVALS_FILE = DATA_DIR / "pending_removals.json"
TICKET_OPENERS_FILE = DATA_DIR / "ticket_openers.json"
# Filename kept from when invites were scored, so existing records carry over.
INVITED_BY_FILE = DATA_DIR / "credited_members.json"
# Legacy: only read once at startup to fold leftover records into INVITED_BY_FILE.
LEGACY_PENDING_CREDITS_FILE = DATA_DIR / "pending_credits.json"
LEGACY_SUSPICIOUS_JOINS_FILE = DATA_DIR / "suspicious_joins.json"
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

