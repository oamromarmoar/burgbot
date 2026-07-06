"""
Discord Referral + Ticket Role-Removal Bot
===========================================

See README.md for setup instructions, required intents, and permissions.

Feature 1 (Referrals):
    - Tracks which invite was used for each new member by diffing cached
      invite use-counts against live counts in on_member_join.
    - Persists per-referrer counts to disk (JSON) so they survive restarts.
    - Grants REFERRAL_ROLE_ID once a referrer hits REFERRALS_NEEDED. Progress
      toward the next reward resets to 0 once the role is later removed
      (Feature 2); a separate lifetime total is kept and never resets.
    - Anti-abuse: each Discord account can only ever be credited once (blocks
      leave/rejoin farming), credit only finalizes after a configurable
      retention window and voids if the member leaves early, and accounts
      younger than a configurable age are queued for manual staff review
      instead of auto-credited (blocks obvious alt-account self-referrals).

Feature 2 (Ticket role removal):
    - Detects "ticket" channels created by a separate ticket bot.
    - When a member with CHEF_ROLE_ID posts in a ticket channel, schedules
      removal of REFERRAL_ROLE_ID from the ticket opener after a delay.
    - Pending removals are persisted to disk and re-armed on startup, so a
      bot restart does not silently drop a scheduled removal.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

# ======================================================================
# CONFIG  (all IDs / thresholds / timing live here)
# ======================================================================

# --- Auth -------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]  # required, no default on purpose

# --- Feature 1: Referrals ---------------------------------------------
REFERRAL_ROLE_ID: int = int(os.environ.get("REFERRAL_ROLE_ID", "0"))
REFERRALS_NEEDED: int = int(os.environ.get("REFERRALS_NEEDED", "5"))

# --- Feature 1: Anti-abuse ---------------------------------------------
# A joining Discord account younger than this is not auto-credited; the join
# is queued for staff review instead (see !suspicious_joins). Defends against
# freshly-created alt accounts used to self-refer. Legitimate new Discord
# users will also get flagged here -- that's intentional, since a bot has no
# way to distinguish "new to Discord" from "made to farm referrals"; staff
# review is the tradeoff instead of silently rejecting or silently accepting.
MIN_ACCOUNT_AGE_SECONDS: int = int(os.environ.get("MIN_ACCOUNT_AGE_SECONDS", str(3 * 24 * 3600)))

# A credited join isn't finalized immediately -- it's held for this long and
# only counted if the member is still in the guild when the timer fires (see
# on_member_remove). This makes "join an alt, get credit, immediately leave"
# cycles worthless, since leaving during the window voids the credit.
MIN_MEMBER_RETENTION_SECONDS: int = int(os.environ.get("MIN_MEMBER_RETENTION_SECONDS", str(24 * 3600)))

# Optional channel to post suspicious-join notifications to, in addition to
# the log. 0 = disabled (still visible via !suspicious_joins and logs).
SUSPICIOUS_LOG_CHANNEL_ID: int = int(os.environ.get("SUSPICIOUS_LOG_CHANNEL_ID", "0"))

# --- Feature 2: Ticket detection --------------------------------------
CHEF_ROLE_ID: int = int(os.environ.get("CHEF_ROLE_ID", "0"))

# Detection mode: "prefix", "category", or "and" (require both to match).
# "and" is the safest default when both a prefix and a category id are
# configured; set the mode explicitly if you only want one criterion.
TICKET_DETECTION_MODE: str = os.environ.get("TICKET_DETECTION_MODE", "and")  # "prefix" | "category" | "and"
TICKET_CHANNEL_PREFIX: str = os.environ.get("TICKET_CHANNEL_PREFIX", "ticket-")
TICKET_CATEGORY_ID: int = int(os.environ.get("TICKET_CATEGORY_ID", "0"))  # 0 = unset

# Delay before the referral role is removed from the ticket opener, once a
# Chef has responded in the ticket.
ROLE_REMOVAL_DELAY_SECONDS: int = int(os.environ.get("ROLE_REMOVAL_DELAY_SECONDS", str(20 * 60)))

# --- Storage paths ------------------------------------------------------
DATA_DIR = Path(os.environ.get("BOT_DATA_DIR", Path(__file__).parent / "data"))
REFERRAL_PROGRESS_FILE = DATA_DIR / "referral_progress.json"
TOTAL_REFERRALS_FILE = DATA_DIR / "total_referrals.json"
PENDING_REMOVALS_FILE = DATA_DIR / "pending_removals.json"
TICKET_OPENERS_FILE = DATA_DIR / "ticket_openers.json"
CREDITED_MEMBERS_FILE = DATA_DIR / "credited_members.json"
PENDING_CREDITS_FILE = DATA_DIR / "pending_credits.json"
SUSPICIOUS_JOINS_FILE = DATA_DIR / "suspicious_joins.json"
GUILD_CONFIG_FILE = DATA_DIR / "guild_config.json"

# --- Command prefix ------------------------------------------------------
COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("referral_ticket_bot")

# ======================================================================
# Per-guild config
# ======================================================================
# Everything above is only the *default* config, used until a server admin
# runs the setup wizard (via !setup, or the prompt posted on_guild_join).
# Once configured, per-guild values are persisted in guild_config.json and
# take precedence over the env-var defaults -- see get_guild_config().
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
}


# ======================================================================
# Small JSON persistence helper
# ======================================================================

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to load %s (%s); starting with default value.", path, exc)
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(path)  # atomic on POSIX and Windows


# ======================================================================
# Bot
# ======================================================================

class ReferralTicketBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}

        # referrer_id (str) -> referrals since their last reward. Resets to 0
        # once the referral role is removed from them (see
        # _perform_role_removal), so they have to earn REFERRALS_NEEDED again
        # for the next reward.
        self.referral_progress: dict[str, int] = _load_json(REFERRAL_PROGRESS_FILE, {})

        # referrer_id (str) -> lifetime referral count. Never resets; purely
        # for stats/leaderboard purposes.
        self.total_referrals: dict[str, int] = _load_json(TOTAL_REFERRALS_FILE, {})

        # channel_id (str) -> opener member_id (str). Populated opportunistically
        # from channel topics / manual overrides; not guaranteed to be complete.
        self.ticket_openers: dict[str, str] = _load_json(TICKET_OPENERS_FILE, {})

        # key "channel_id:member_id" -> {"guild_id", "channel_id", "member_id", "remove_at"}
        self.pending_removals: dict[str, dict] = _load_json(PENDING_REMOVALS_FILE, {})
        self._removal_tasks: dict[str, asyncio.Task] = {}

        # member_id (str) -> referrer_id (str). Permanent record of every member
        # who has ever been credited, to anyone, ever. Enforces "one credit per
        # Discord account for its lifetime" regardless of leave/rejoin or which
        # invite is used the second time around.
        self.credited_members: dict[str, str] = _load_json(CREDITED_MEMBERS_FILE, {})

        # member_id (str) -> {"guild_id", "member_id", "referrer_id", "credit_at"}
        # Joins that passed the account-age check but are still within the
        # retention window; finalized (or dropped, if the member left) by
        # _pending_credit_worker.
        self.pending_credits: dict[str, dict] = _load_json(PENDING_CREDITS_FILE, {})
        self._credit_tasks: dict[str, asyncio.Task] = {}

        # member_id (str) -> {"guild_id", "member_id", "referrer_id", "account_age_seconds", "flagged_at"}
        # Joins that failed the account-age check, awaiting manual staff review
        # via !approve_referral / !deny_referral.
        self.suspicious_joins: dict[str, dict] = _load_json(SUSPICIOUS_JOINS_FILE, {})

        # guild_id (str) -> partial config dict, set via the !setup wizard.
        # Merged over DEFAULT_GUILD_CONFIG by get_guild_config(); only fields
        # a guild has actually configured are stored here.
        self.guild_configs: dict[str, dict] = _load_json(GUILD_CONFIG_FILE, {})

    # ------------------------------------------------------------------
    # Per-guild config
    # ------------------------------------------------------------------
    def get_guild_config(self, guild_id: int) -> dict:
        cfg = dict(DEFAULT_GUILD_CONFIG)
        cfg.update(self.guild_configs.get(str(guild_id), {}))
        return cfg

    def update_guild_config(self, guild_id: int, **fields):
        key = str(guild_id)
        self.guild_configs.setdefault(key, {})
        self.guild_configs[key].update(fields)
        _save_json(GUILD_CONFIG_FILE, self.guild_configs)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _save_referral_progress(self):
        _save_json(REFERRAL_PROGRESS_FILE, self.referral_progress)

    def _save_total_referrals(self):
        _save_json(TOTAL_REFERRALS_FILE, self.total_referrals)

    def _save_pending_removals(self):
        _save_json(PENDING_REMOVALS_FILE, self.pending_removals)

    def _save_ticket_openers(self):
        _save_json(TICKET_OPENERS_FILE, self.ticket_openers)

    def _save_credited_members(self):
        _save_json(CREDITED_MEMBERS_FILE, self.credited_members)

    def _save_pending_credits(self):
        _save_json(PENDING_CREDITS_FILE, self.pending_credits)

    def _save_suspicious_joins(self):
        _save_json(SUSPICIOUS_JOINS_FILE, self.suspicious_joins)

    # ------------------------------------------------------------------
    # Startup: re-arm any pending removals/credits that survived a restart
    # ------------------------------------------------------------------
    async def setup_hook(self):
        for key, entry in list(self.pending_removals.items()):
            self._arm_removal_task(key, entry)
        for key, entry in list(self.pending_credits.items()):
            self._arm_credit_task(key, entry)

    def _arm_removal_task(self, key: str, entry: dict):
        delay = max(0.0, entry["remove_at"] - time.time())
        task = asyncio.create_task(self._removal_worker(key, entry, delay))
        self._removal_tasks[key] = task

    def _arm_credit_task(self, key: str, entry: dict):
        delay = max(0.0, entry["credit_at"] - time.time())
        task = asyncio.create_task(self._pending_credit_worker(key, entry, delay))
        self._credit_tasks[key] = task

    # ------------------------------------------------------------------
    # Invite caching (Feature 1)
    # ------------------------------------------------------------------
    async def cache_guild_invites(self, guild: discord.Guild):
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            log.warning("Missing permission to fetch invites for guild %s (%s).", guild.name, guild.id)
            return
        except discord.HTTPException as exc:
            log.warning("Failed to fetch invites for guild %s: %s", guild.id, exc)
            return
        self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in invites}

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        for guild in self.guilds:
            await self.cache_guild_invites(guild)
        log.info("Cached invites for %d guild(s).", len(self.invite_cache))

    async def on_guild_join(self, guild: discord.Guild):
        await self.cache_guild_invites(guild)
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages),
                None,
            )
        if channel is None:
            log.warning("Joined guild %s but found no channel to post the setup prompt in.", guild.id)
            return
        embed = discord.Embed(
            title="Thanks for adding me!",
            description=(
                "An **administrator** needs to run setup before referral tracking or ticket "
                "role removal will do anything. Click below, or run `!setup` any time to "
                "(re)configure."
            ),
            color=discord.Color.blurple(),
        )
        try:
            await channel.send(embed=embed, view=SetupPromptView(self))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Failed to post setup prompt in guild %s: %s", guild.id, exc)

    async def on_invite_create(self, invite: discord.Invite):
        if invite.guild is None:
            return
        self.invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    async def on_invite_delete(self, invite: discord.Invite):
        if invite.guild is None:
            return
        self.invite_cache.get(invite.guild.id, {}).pop(invite.code, None)

    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cached = self.invite_cache.get(guild.id, {})

        try:
            live_invites = await guild.invites()
        except discord.Forbidden:
            log.warning("Missing permission to fetch invites in guild %s; cannot attribute join.", guild.id)
            return
        except discord.HTTPException as exc:
            log.warning("Failed to fetch invites for guild %s: %s", guild.id, exc)
            return

        used_invite: Optional[discord.Invite] = None
        for invite in live_invites:
            prior_uses = cached.get(invite.code, 0)
            if (invite.uses or 0) > prior_uses:
                used_invite = invite
                break

        # Refresh the cache to the current state regardless of outcome, so the
        # next join diffs against accurate numbers.
        self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in live_invites}

        if used_invite is None or used_invite.inviter is None:
            # KNOWN LIMITATION: if two people joined via different invites at the
            # same moment, or the invite used was a vanity URL (which never shows
            # up in guild.invites() and has no use counter), we cannot reliably
            # attribute this join. We simply skip crediting anyone rather than
            # guessing.
            log.info("Could not attribute join of %s to a specific invite.", member)
            return

        await self._attribute_join(guild, used_invite.inviter, member)

    async def _attribute_join(self, guild: discord.Guild, referrer: discord.abc.User, new_member: discord.Member):
        member_key = str(new_member.id)
        cfg = self.get_guild_config(guild.id)

        # Anti leave/rejoin farming: a given Discord account can only ever be
        # credited once, period -- no matter how many times it leaves and
        # rejoins, and no matter whose invite it uses next time.
        if member_key in self.credited_members:
            log.info("%s was already credited previously; ignoring repeat join.", new_member)
            return
        if member_key in self.pending_credits or member_key in self.suspicious_joins:
            log.info("%s already has a pending credit/review; ignoring repeat join.", new_member)
            return

        account_age = time.time() - new_member.created_at.timestamp()
        if account_age < cfg["min_account_age_seconds"]:
            await self._flag_suspicious(guild, referrer, new_member, account_age)
            return

        entry = {
            "guild_id": guild.id,
            "member_id": new_member.id,
            "referrer_id": referrer.id,
            "credit_at": time.time() + cfg["min_member_retention_seconds"],
        }
        self.pending_credits[member_key] = entry
        self._save_pending_credits()
        self._arm_credit_task(member_key, entry)
        log.info(
            "Queued referral credit for %s (via %s), finalizing in %ds if they stay.",
            new_member, referrer, cfg["min_member_retention_seconds"],
        )

    async def _flag_suspicious(
        self, guild: discord.Guild, referrer: discord.abc.User, new_member: discord.Member, account_age: float
    ):
        member_key = str(new_member.id)
        cfg = self.get_guild_config(guild.id)
        entry = {
            "guild_id": guild.id,
            "member_id": new_member.id,
            "referrer_id": referrer.id,
            "account_age_seconds": account_age,
            "flagged_at": time.time(),
        }
        self.suspicious_joins[member_key] = entry
        self._save_suspicious_joins()
        log.warning(
            "Flagged join of %s (account age %.1fh, below %.1fh minimum) referred by %s for manual review.",
            new_member, account_age / 3600, cfg["min_account_age_seconds"] / 3600, referrer,
        )
        if cfg["suspicious_log_channel_id"]:
            channel = guild.get_channel(cfg["suspicious_log_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        f"Suspicious referral join: {new_member.mention} (account age "
                        f"{account_age / 3600:.1f}h) referred by {referrer.mention}. "
                        f"Review with `!suspicious_joins`, then `!approve_referral` or `!deny_referral`."
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Failed to post suspicious-join notice: %s", exc)

    async def on_member_remove(self, member: discord.Member):
        # If this member left before their retention window elapsed, void the
        # pending credit -- "join, get credited, immediately leave" nets nothing.
        key = str(member.id)
        entry = self.pending_credits.pop(key, None)
        if entry is not None:
            self._save_pending_credits()
            task = self._credit_tasks.pop(key, None)
            if task is not None:
                task.cancel()
            log.info("%s left before the retention window elapsed; credit voided.", member)

    async def _pending_credit_worker(self, key: str, entry: dict, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._finalize_credit(entry)
        finally:
            self.pending_credits.pop(key, None)
            self._save_pending_credits()
            self._credit_tasks.pop(key, None)

    async def _finalize_credit(self, entry: dict):
        guild = self.get_guild(entry["guild_id"])
        if guild is None:
            log.warning("Guild %s no longer available; dropping pending credit.", entry["guild_id"])
            return
        member = guild.get_member(entry["member_id"])
        if member is None:
            # on_member_remove should have already voided this, but double-check
            # directly against the guild in case that event was missed.
            log.info("Member %s no longer in guild %s; not crediting.", entry["member_id"], guild.id)
            return

        member_key = str(entry["member_id"])
        referrer_key = str(entry["referrer_id"])
        self.credited_members[member_key] = referrer_key
        self._save_credited_members()
        await self._credit_referrer(guild, entry["referrer_id"], member)

    async def _credit_referrer(self, guild: discord.Guild, referrer_id: int, new_member: discord.Member):
        key = str(referrer_id)
        cfg = self.get_guild_config(guild.id)

        self.total_referrals[key] = self.total_referrals.get(key, 0) + 1
        self._save_total_referrals()

        self.referral_progress[key] = self.referral_progress.get(key, 0) + 1
        progress = self.referral_progress[key]
        self._save_referral_progress()

        log.info(
            "Referrer %s: %d/%d toward next reward (%d lifetime, last: %s joined).",
            referrer_id, progress, cfg["referrals_needed"], self.total_referrals[key], new_member,
        )

        if cfg["referral_role_id"] and progress >= cfg["referrals_needed"]:
            await self._grant_referral_role(guild, referrer_id)

    async def _grant_referral_role(self, guild: discord.Guild, referrer_id: int):
        cfg = self.get_guild_config(guild.id)
        role = guild.get_role(cfg["referral_role_id"])
        if role is None:
            log.warning("referral_role_id %s not found in guild %s.", cfg["referral_role_id"], guild.id)
            return
        member = guild.get_member(referrer_id)
        if member is None:
            try:
                member = await guild.fetch_member(referrer_id)
            except discord.NotFound:
                log.warning("Referrer %s is no longer in guild %s.", referrer_id, guild.id)
                return
            except discord.HTTPException as exc:
                log.warning("Failed to fetch referrer %s: %s", referrer_id, exc)
                return
        if role in member.roles:
            return
        try:
            await member.add_roles(role, reason=f"Reached {cfg['referrals_needed']} referrals")
            log.info("Granted referral role to %s.", member)
        except discord.Forbidden:
            log.error(
                "Forbidden: cannot grant role %s to %s. "
                "Check the bot's top role is above the referral role and it has Manage Roles.",
                role, member,
            )
        except discord.HTTPException as exc:
            log.error("HTTPException granting role to %s: %s", member, exc)

    # ------------------------------------------------------------------
    # Ticket detection (Feature 2)
    # ------------------------------------------------------------------
    def is_ticket_channel(self, channel: discord.abc.GuildChannel) -> bool:
        cfg = self.get_guild_config(channel.guild.id)
        prefix = cfg["ticket_channel_prefix"]
        category_id = cfg["ticket_category_id"]

        name_match = channel.name.startswith(prefix) if prefix else False
        category_match = category_id != 0 and getattr(channel, "category_id", None) == category_id

        if cfg["ticket_detection_mode"] == "prefix":
            return name_match
        if cfg["ticket_detection_mode"] == "category":
            return category_match
        # "and" (default): require both configured criteria to match. If one of
        # the two criteria is unconfigured (empty prefix / category id 0), fall
        # back to whichever single criterion *is* configured.
        if prefix and category_id:
            return name_match and category_match
        return name_match or category_match

    _TOPIC_OPENER_RE = re.compile(r"<@!?(?P<id>\d+)>")

    def _infer_opener_from_overwrites(self, channel: discord.TextChannel) -> Optional[discord.Member]:
        """
        A private ticket channel has to grant access to its opener somehow, and
        the only mechanism Discord provides for scoping a channel to one
        specific non-role-based user is a per-member permission overwrite --
        that's how Ticket Tool (and virtually every other ticket bot) makes the
        channel visible to just that person plus whatever staff role(s) it
        adds. Reading that overwrite tells us who the channel was actually
        created for, independent of what roles they currently hold (unlike the
        role-holder fallback below, which can be fooled by staff or other
        referral-role holders who simply happen to have channel access).
        Tradeoff: if the ticket bot *also* grants individual overwrites to
        specific staff members instead of via a role, we can't tell them apart
        from the opener and bail out rather than guess.
        """
        chef_role_id = self.get_guild_config(channel.guild.id)["chef_role_id"]
        candidates = [
            target for target in channel.overwrites
            if isinstance(target, discord.Member)
            and not target.bot
            and (not chef_role_id or chef_role_id not in (r.id for r in target.roles))
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            log.warning(
                "Ambiguous ticket opener via permission overwrites for channel %s: %d candidates.",
                channel.id, len(candidates),
            )
        return None

    async def get_ticket_opener(self, channel: discord.TextChannel) -> Optional[discord.Member]:
        """
        Determine who opened a ticket. Tried in order, most to least reliable:

        1. Stored channel -> opener mapping (self.ticket_openers). Most
           reliable source *if* populated -- normally it already is, because
           on_guild_channel_create fills it in via method 2 the moment the
           ticket channel is created.
        2. Per-member permission overwrites (see _infer_opener_from_overwrites).
           This is the primary automatic method: it reflects who Ticket Tool
           actually granted channel access to, not a role they happen to hold.
        3. The channel topic, if it contains a user mention (some ticket bots
           can be configured to include one). Fragile: depends entirely on
           that specific bot/config, and breaks silently if absent.
        4. Last-resort fallback: a single referral-role holder present in the
           channel. This is the weakest signal -- it assumes the opener is the
           *only* referral-role holder with access, which is not guaranteed
           (e.g. staff also holding that role, or an unrelated past referrer
           who can still see the channel). Only used if nothing else matched,
           and only acts if exactly one candidate is found.
        """
        guild = channel.guild

        stored_id = self.ticket_openers.get(str(channel.id))
        if stored_id:
            member = guild.get_member(int(stored_id))
            if member:
                return member

        inferred = self._infer_opener_from_overwrites(channel)
        if inferred:
            self.ticket_openers[str(channel.id)] = str(inferred.id)
            self._save_ticket_openers()
            return inferred

        if channel.topic:
            match = self._TOPIC_OPENER_RE.search(channel.topic)
            if match:
                member = guild.get_member(int(match.group("id")))
                if member:
                    self.ticket_openers[str(channel.id)] = str(member.id)
                    self._save_ticket_openers()
                    return member

        referral_role_id = self.get_guild_config(guild.id)["referral_role_id"]
        if referral_role_id:
            role = guild.get_role(referral_role_id)
            if role:
                candidates = [m for m in channel.members if role in m.roles and not m.bot]
                if len(candidates) == 1:
                    log.warning(
                        "Falling back to weakest signal (role-holder heuristic) for channel %s.",
                        channel.id,
                    )
                    return candidates[0]
                if len(candidates) > 1:
                    log.warning(
                        "Ambiguous ticket opener for channel %s: multiple referral-role holders present.",
                        channel.id,
                    )

        log.warning("Could not determine ticket opener for channel %s.", channel.id)
        return None

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        # Opportunistically populate the opener map as soon as a ticket
        # channel appears, since Ticket Tool sets the per-member permission
        # overwrite atomically at creation time -- this means get_ticket_opener()
        # almost always finds the answer already stored by the time a Chef
        # message triggers it later.
        if isinstance(channel, discord.TextChannel) and self.is_ticket_channel(channel):
            inferred = self._infer_opener_from_overwrites(channel)
            if inferred:
                self.ticket_openers[str(channel.id)] = str(inferred.id)
                self._save_ticket_openers()
                return
            if channel.topic:
                match = self._TOPIC_OPENER_RE.search(channel.topic)
                if match:
                    self.ticket_openers[str(channel.id)] = match.group("id")
                    self._save_ticket_openers()

    async def on_message(self, message: discord.Message):
        await self.process_commands(message)

        if message.author.bot or message.guild is None:
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if not self.is_ticket_channel(message.channel):
            return
        cfg = self.get_guild_config(message.guild.id)
        if not cfg["chef_role_id"] or cfg["chef_role_id"] not in (r.id for r in message.author.roles):
            return

        opener = await self.get_ticket_opener(message.channel)
        if opener is None:
            return

        key = f"{message.channel.id}:{opener.id}"
        if key in self.pending_removals or key in self._removal_tasks:
            return  # already scheduled for this member/ticket -- do not double-schedule

        entry = {
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "member_id": opener.id,
            "remove_at": time.time() + cfg["role_removal_delay_seconds"],
        }
        self.pending_removals[key] = entry
        self._save_pending_removals()
        self._arm_removal_task(key, entry)
        log.info(
            "Scheduled referral-role removal for %s in #%s in %ds.",
            opener, message.channel.name, cfg["role_removal_delay_seconds"],
        )

    async def _removal_worker(self, key: str, entry: dict, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._perform_role_removal(entry)
        finally:
            self.pending_removals.pop(key, None)
            self._save_pending_removals()
            self._removal_tasks.pop(key, None)

    async def _perform_role_removal(self, entry: dict):
        guild = self.get_guild(entry["guild_id"])
        if guild is None:
            log.warning("Guild %s no longer available; skipping scheduled removal.", entry["guild_id"])
            return
        referral_role_id = self.get_guild_config(guild.id)["referral_role_id"]
        role = guild.get_role(referral_role_id)
        if role is None:
            log.warning("referral_role_id %s not found in guild %s.", referral_role_id, guild.id)
            return
        member = guild.get_member(entry["member_id"])
        if member is None:
            try:
                member = await guild.fetch_member(entry["member_id"])
            except discord.NotFound:
                log.info("Member %s left the guild before scheduled removal ran.", entry["member_id"])
                return
            except discord.HTTPException as exc:
                log.warning("Failed to fetch member %s: %s", entry["member_id"], exc)
                return
        if role not in member.roles:
            return
        try:
            await member.remove_roles(role, reason="Ticket handled by Chef; referral role removal window elapsed")
            log.info("Removed referral role from %s.", member)
            # Prize claimed: reset progress toward the *next* reward. Lifetime
            # total_referrals is untouched, so historical/leaderboard counts
            # still reflect everything they've ever referred.
            member_key = str(member.id)
            if member_key in self.referral_progress:
                self.referral_progress[member_key] = 0
                self._save_referral_progress()
        except discord.Forbidden:
            log.error(
                "Forbidden: cannot remove role %s from %s. "
                "Check the bot's top role is above the referral role and it has Manage Roles.",
                role, member,
            )
        except discord.HTTPException as exc:
            log.error("HTTPException removing role from %s: %s", member, exc)


# ======================================================================
# Setup wizard UI (admin-only, dropdown-driven server configuration)
# ======================================================================
# Triggered automatically on_guild_join (a prompt is posted for an admin to
# click) and re-runnable any time via the admin-only !setup command. All
# selections use Discord's native role/channel pickers or preset dropdowns --
# no IDs to copy/paste. Saved settings apply immediately via
# get_guild_config(); no restart required.

ADMIN_ONLY_MESSAGE = "You need the **Administrator** permission to do this."

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


def _format_duration(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "Disabled"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


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


def _build_setup_embed(guild: discord.Guild, config: dict, step: int, total_steps: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"Referral + Ticket Bot Setup ({step}/{total_steps})",
        description="Admins only. Pick values with the dropdowns below, then continue.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Referral role", value=_role_display(guild, config.get("referral_role_id")))
    embed.add_field(name="Chef role", value=_role_display(guild, config.get("chef_role_id")))
    embed.add_field(name="Ticket category", value=_channel_display(guild, config.get("ticket_category_id")))
    embed.add_field(name="Ticket name prefix", value=f"`{config.get('ticket_channel_prefix') or '(none)'}`")
    embed.add_field(name="Referrals needed", value=str(config.get("referrals_needed", REFERRALS_NEEDED)))
    embed.add_field(name="Role removal delay", value=_format_duration(config.get("role_removal_delay_seconds")))
    embed.add_field(name="Min account age", value=_format_duration(config.get("min_account_age_seconds")))
    embed.add_field(name="Min member retention", value=_format_duration(config.get("min_member_retention_seconds")))
    embed.add_field(
        name="Suspicious-join log channel", value=_channel_display(guild, config.get("suspicious_log_channel_id"))
    )
    return embed


def _make_duration_options(choices, current) -> list:
    return [
        discord.SelectOption(label=label, value=str(seconds), default=(int(current or 0) == seconds))
        for label, seconds in choices
    ]


class AdminOnlyView(discord.ui.View):
    def __init__(self, bot: "ReferralTicketBot", guild: discord.Guild, config: dict, *, timeout: float = 300):
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
            await self.message.edit(content="Setup timed out -- run `!setup` again to continue.", view=self)
        except (discord.Forbidden, discord.HTTPException):
            pass


class TicketPrefixModal(discord.ui.Modal, title="Ticket Channel Name Prefix"):
    prefix: discord.ui.TextInput = discord.ui.TextInput(
        label="Channel name must start with (blank = disabled)",
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


class SetupStep1View(AdminOnlyView):
    """Step 1: roles + ticket detection."""

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 1, 3)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Referral role (granted after enough referrals)",
        min_values=0, max_values=1, row=0,
    )
    async def referral_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if select.values:
            self.config["referral_role_id"] = select.values[0].id
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="Chef role (triggers ticket role removal)",
        min_values=0, max_values=1, row=1,
    )
    async def chef_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        if select.values:
            self.config["chef_role_id"] = select.values[0].id
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Ticket category (optional)",
        channel_types=[discord.ChannelType.category], min_values=0, max_values=1, row=2,
    )
    async def ticket_category_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if select.values:
            self.config["ticket_category_id"] = select.values[0].id
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
                default=(config.get("referrals_needed", REFERRALS_NEEDED) == n),
            )
            for n in REFERRALS_NEEDED_CHOICES
        ]
        self.removal_delay_select.options = _make_duration_options(
            DELAY_CHOICES, config.get("role_removal_delay_seconds", ROLE_REMOVAL_DELAY_SECONDS)
        )
        self.account_age_select.options = _make_duration_options(
            ACCOUNT_AGE_CHOICES, config.get("min_account_age_seconds", MIN_ACCOUNT_AGE_SECONDS)
        )
        self.retention_select.options = _make_duration_options(
            RETENTION_CHOICES, config.get("min_member_retention_seconds", MIN_MEMBER_RETENTION_SECONDS)
        )

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 2, 3)

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
    """Step 3: suspicious-join logging + save."""

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, placeholder="Channel for suspicious-join alerts (optional)",
        channel_types=[discord.ChannelType.text], min_values=0, max_values=1, row=0,
    )
    async def suspicious_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        if select.values:
            self.config["suspicious_log_channel_id"] = select.values[0].id
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    def render_embed(self) -> discord.Embed:
        return _build_setup_embed(self.guild, self.config, 3, 3)

    @discord.ui.button(label="◀ Back", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetupStep2View(self.bot, self.guild, self.config)
        view.message = self.message
        await interaction.response.edit_message(embed=view.render_embed(), view=view)

    @discord.ui.button(label="✅ Save Configuration", style=discord.ButtonStyle.success, row=1)
    async def save_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.bot.update_guild_config(self.guild.id, **self.config)
        for item in self.children:
            item.disabled = True
        embed = self.render_embed()
        embed.color = discord.Color.green()
        embed.description = "Configuration saved -- takes effect immediately, no restart needed."
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()


class SetupPromptView(discord.ui.View):
    """Posted on_guild_join. Not restart-persistent (see README) -- !setup is
    the reliable fallback if the bot restarts before this gets clicked."""

    def __init__(self, bot: "ReferralTicketBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return False

    @discord.ui.button(label="Start Setup", style=discord.ButtonStyle.primary, emoji="⚙")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        config = dict(self.bot.get_guild_config(guild.id))
        view = SetupStep1View(self.bot, guild, config)
        await interaction.response.edit_message(content=None, embed=view.render_embed(), view=view)
        view.message = interaction.message


# ======================================================================
# Intents
# ======================================================================
# Privileged intents (must also be enabled in the Developer Portal ->
# Bot -> Privileged Gateway Intents):
#   - members:         required for on_member_join and accurate member/role state
#   - message_content: required to receive message.content (not strictly
#                       needed by this bot's logic today, since Feature 2 only
#                       inspects the author's roles and channel, but enabled
#                       per spec / for future use)
# Non-privileged intents used:
#   - guilds:  required baseline for guild/channel/role events
#   - invites: required for on_invite_create / on_invite_delete
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.invites = True

bot = ReferralTicketBot(command_prefix=COMMAND_PREFIX, intents=intents)


# ======================================================================
# Commands
# ======================================================================

@bot.command(name="setup")
@commands.has_permissions(administrator=True)
async def setup_command(ctx: commands.Context):
    """(Re)configure this server's referral/ticket settings via dropdowns."""
    config = dict(bot.get_guild_config(ctx.guild.id))
    view = SetupStep1View(bot, ctx.guild, config)
    message = await ctx.send(embed=view.render_embed(), view=view)
    view.message = message


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use that command.")
        return
    if isinstance(error, (commands.CommandNotFound, commands.MissingRequiredArgument, commands.BadArgument)):
        return
    log.error("Unhandled command error in %s: %s", ctx.command, error, exc_info=error)


@bot.command(name="referrals_count")
async def referrals_count(ctx: commands.Context, member: discord.Member = None):
    target = member or ctx.author
    key = str(target.id)
    progress = bot.referral_progress.get(key, 0)
    total = bot.total_referrals.get(key, 0)
    referrals_needed = bot.get_guild_config(ctx.guild.id)["referrals_needed"]
    await ctx.send(
        f"{target.mention} has {total} referral(s) lifetime, "
        f"{progress}/{referrals_needed} toward their next reward."
    )


@bot.command(name="set_ticket_opener")
@commands.has_permissions(manage_roles=True)
async def set_ticket_opener(ctx: commands.Context, channel: discord.TextChannel, member: discord.Member):
    """Manually record who opened a ticket, for channels the automatic
    topic-parsing heuristic can't figure out."""
    bot.ticket_openers[str(channel.id)] = str(member.id)
    bot._save_ticket_openers()
    await ctx.send(f"Recorded {member.mention} as the opener of {channel.mention}.")


@bot.command(name="suspicious_joins")
@commands.has_permissions(manage_roles=True)
async def suspicious_joins(ctx: commands.Context):
    """List joins flagged for account age, awaiting manual review."""
    if not bot.suspicious_joins:
        await ctx.send("No suspicious joins pending review.")
        return
    lines = []
    for member_key, entry in bot.suspicious_joins.items():
        age_h = entry["account_age_seconds"] / 3600
        lines.append(f"<@{member_key}> (account age {age_h:.1f}h) referred by <@{entry['referrer_id']}>")
    await ctx.send("Suspicious joins pending review:\n" + "\n".join(lines))


@bot.command(name="approve_referral")
@commands.has_permissions(manage_roles=True)
async def approve_referral(ctx: commands.Context, member: discord.Member):
    """Manually credit a flagged join after staff confirms it's legitimate."""
    key = str(member.id)
    entry = bot.suspicious_joins.pop(key, None)
    if entry is None:
        await ctx.send(f"No suspicious join on record for {member.mention}.")
        return
    bot._save_suspicious_joins()
    bot.credited_members[key] = str(entry["referrer_id"])
    bot._save_credited_members()
    await bot._credit_referrer(ctx.guild, entry["referrer_id"], member)
    await ctx.send(f"Approved and credited referral for {member.mention}.")


@bot.command(name="deny_referral")
@commands.has_permissions(manage_roles=True)
async def deny_referral(ctx: commands.Context, member: discord.Member):
    """Discard a flagged join without crediting anyone."""
    key = str(member.id)
    if bot.suspicious_joins.pop(key, None) is None:
        await ctx.send(f"No suspicious join on record for {member.mention}.")
        return
    bot._save_suspicious_joins()
    await ctx.send(f"Denied referral credit for {member.mention}.")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
