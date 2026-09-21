"""
BurgBot core: persistent state, gateway event handling, and the business
logic for invite logging, ticket pings, ticket role removal, and
restart-safe timers (removals, reminders).

Commands and interactive UI live in the cogs; they call into the methods
defined here.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import storage
from config import DATA_DIR, DEFAULT_GUILD_CONFIG, EXTENSIONS
from storage import (
    GUILD_CONFIG_FILE,
    INVITED_BY_FILE,
    LEGACY_PENDING_CREDITS_FILE,
    LEGACY_SUSPICIOUS_JOINS_FILE,
    PENDING_REMOVALS_FILE,
    REMINDERS_FILE,
    SYNCED_COMMANDS_FILE,
    TICKET_OPENERS_FILE,
    WARNINGS_FILE,
    POLLS_FILE,
    load_json,
    save_json,
)
from ui import error_embed

log = logging.getLogger("burgbot")


class BurgBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.launch_time = discord.utils.utcnow()
        self._synced_commands_hash: Optional[str] = load_json(SYNCED_COMMANDS_FILE, {}).get("hash")

        # guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}

        # channel_id (str) -> opener member_id (str)
        self.ticket_openers: dict[str, str] = load_json(TICKET_OPENERS_FILE, {})

        # key "channel_id:member_id" -> {"guild_id", "channel_id", "member_id", "remove_at"}
        self.pending_removals: dict[str, dict] = load_json(PENDING_REMOVALS_FILE, {})
        self._removal_tasks: dict[str, asyncio.Task] = {}

        # member_id (str) -> inviter_id (str), from the member's latest attributed join.
        self.invited_by: dict[str, str] = load_json(INVITED_BY_FILE, {})
        self._fold_legacy_invite_records()

        # guild_id (str) -> partial config dict set via /setup.
        self.guild_configs: dict[str, dict] = load_json(GUILD_CONFIG_FILE, {})

        # guild_id (str) -> {user_id (str): [{"mod_id", "reason", "at"}]}
        self.warnings: dict[str, dict] = load_json(WARNINGS_FILE, {})

        # reminder_id -> {"user_id", "guild_id", "channel_id", "remind_at", "text", "created_at"}
        self.reminders: dict[str, dict] = load_json(REMINDERS_FILE, {})
        self._reminder_tasks: dict[str, asyncio.Task] = {}

        # poll_id -> {"guild_id", "channel_id", "message_id", "author_id",
        #             "question", "options", "votes", "end_at", "created_at"}
        # (interactive lifecycle managed by cogs/fun.py)
        self.polls: dict[str, dict] = load_json(POLLS_FILE, {})

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
        save_json(GUILD_CONFIG_FILE, self.guild_configs)

    def _fold_legacy_invite_records(self):
        """Joins that were still awaiting credit or staff review when invite
        scoring was removed never made it into invited_by; fold them in so
        /who_invited keeps answering for those members."""
        added = 0
        for path in (LEGACY_PENDING_CREDITS_FILE, LEGACY_SUSPICIOUS_JOINS_FILE):
            for member_id, entry in load_json(path, {}).items():
                if member_id not in self.invited_by:
                    self.invited_by[member_id] = str(entry["referrer_id"])
                    added += 1
        if added:
            self.save_invited_by()
            log.info("Folded %d leftover pending/flagged join(s) into the invite record.", added)

    # ------------------------------------------------------------------
    # Activity log channel
    # ------------------------------------------------------------------
    async def send_log(self, guild: discord.Guild, message: str, *, color: Optional[discord.Color] = None):
        """Post a notable event to this guild's configured log channel, if any.
        Always goes to the process log regardless of whether a channel is set."""
        log.info("[guild %s] %s", guild.id, message)
        channel_id = self.get_guild_config(guild.id)["log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(description=message, color=color or discord.Color.blurple())
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Failed to post log message in guild %s: %s", guild.id, exc)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _save_pending_removals(self):
        save_json(PENDING_REMOVALS_FILE, self.pending_removals)

    def save_ticket_openers(self):
        save_json(TICKET_OPENERS_FILE, self.ticket_openers)

    def save_invited_by(self):
        save_json(INVITED_BY_FILE, self.invited_by)

    def save_warnings(self):
        save_json(WARNINGS_FILE, self.warnings)

    def _save_reminders(self):
        save_json(REMINDERS_FILE, self.reminders)

    def save_polls(self):
        save_json(POLLS_FILE, self.polls)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def _log_data_dir_status(self):
        """Loudly report what persisted state (if any) this boot found, so a
        wiped/misconfigured data volume shows up in the deploy logs instead
        of silently starting everyone over at zero."""
        guild_count = len(self.guild_configs)
        invited_count = len(self.invited_by)
        log.info(
            "Data directory: %s (exists=%s). Loaded %d guild config(s), %d invite record(s).",
            DATA_DIR.resolve(), DATA_DIR.exists(), guild_count, invited_count,
        )
        if guild_count == 0 and invited_count == 0:
            log.warning(
                "No persisted data found at startup (no guild config or invite records). "
                "If this bot has been configured/used before, this means persistent storage "
                "isn't attached correctly on this host -- check that BOT_DATA_DIR points at a volume "
                "mount that survives redeploys, not the container's ephemeral disk. If this really is "
                "the first-ever boot, ignore this."
            )

    async def setup_hook(self):
        self._log_data_dir_status()

        for extension in EXTENSIONS:
            await self.load_extension(extension)
        log.info("Loaded %d extension(s).", len(EXTENSIONS))

        # Re-arm every timer that survived the restart.
        for key, entry in list(self.pending_removals.items()):
            self._arm_removal_task(key, entry)
        for rid, entry in list(self.reminders.items()):
            self._arm_reminder_task(rid, entry)

        try:
            count = await self.sync_commands()
        except (discord.HTTPException, app_commands.AppCommandError, discord.ClientException) as exc:
            log.error("Failed to sync slash command tree on startup: %s", exc)
        else:
            if count:
                log.info("Synced %d global slash command(s).", count)
            else:
                log.info("Slash command tree unchanged since last sync; skipped.")

    def _command_tree_hash(self) -> str:
        payload = [c.to_dict(self.tree) for c in self.tree.get_commands()]
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    async def sync_commands(self, *, force: bool = False) -> int:
        """Sync the slash-command tree with Discord. Skips the actual API call
        (and the bulk-overwrite it costs against Discord's rate limit) when the
        command definitions haven't changed since the last successful sync,
        unless force=True. Raises whatever the underlying tree.sync() raises;
        callers decide how to handle/report that."""
        current_hash = self._command_tree_hash()
        if not force and current_hash == self._synced_commands_hash:
            return 0
        synced = await self.tree.sync()
        self._synced_commands_hash = current_hash
        save_json(SYNCED_COMMANDS_FILE, {"hash": current_hash})
        return len(synced)

    def _arm_removal_task(self, key: str, entry: dict):
        delay = max(0.0, entry["remove_at"] - time.time())
        self._removal_tasks[key] = asyncio.create_task(self._removal_worker(key, entry, delay))

    def _arm_reminder_task(self, rid: str, entry: dict):
        delay = max(0.0, entry["remind_at"] - time.time())
        self._reminder_tasks[rid] = asyncio.create_task(self._reminder_worker(rid, entry, delay))

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        for guild in self.guilds:
            await self.cache_guild_invites(guild)
        log.info("Cached invites for %d guild(s).", len(self.invite_cache))
        try:
            await self.change_presence(
                activity=discord.Activity(type=discord.ActivityType.listening, name="/help")
            )
        except (discord.HTTPException, ConnectionError):
            pass

    # ------------------------------------------------------------------
    # Invite caching / attribution
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
        from cogs.admin import SetupPromptView  # local import: cogs load after core

        embed = discord.Embed(
            title="Thanks for adding me! 🍔",
            description=(
                "An **administrator** needs to run setup before invite logging, ticket "
                "pings, ticket role removal, or welcome messages will do anything. Click below, or run "
                "`/setup` any time to (re)configure.\n\nRun `/help` to see everything I can do."
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

        # Refresh the cache regardless of outcome so the next join diffs
        # against accurate numbers.
        self.invite_cache[guild.id] = {invite.code: invite.uses or 0 for invite in live_invites}

        if used_invite is None or used_invite.inviter is None:
            # KNOWN LIMITATION: simultaneous joins via different invites, or a
            # vanity URL (which never appears in guild.invites()), cannot be
            # attributed reliably. Skip logging an inviter rather than guess.
            log.info("Could not attribute join of %s to a specific invite.", member)
            return

        await self._record_invite(guild, used_invite, member)

    async def _record_invite(self, guild: discord.Guild, invite: discord.Invite, new_member: discord.Member):
        """Remember and log who invited whom, for staff supervision. Nothing is
        scored or rewarded; young accounts are flagged so staff can take a look."""
        inviter = invite.inviter
        member_key = str(new_member.id)
        previous_id = self.invited_by.get(member_key)
        self.invited_by[member_key] = str(inviter.id)
        self.save_invited_by()

        cfg = self.get_guild_config(guild.id)
        account_age = time.time() - new_member.created_at.timestamp()
        min_age = cfg["min_account_age_seconds"]
        young = bool(min_age) and account_age < min_age

        message = f"📨 {new_member.mention} joined via {inviter.mention}'s invite `{invite.code}`."
        if previous_id:
            message += f" (Rejoin — previously invited by <@{previous_id}>.)"
        if young:
            message += (
                f"\n⚠️ Young account: {account_age / 3600:.1f}h old "
                f"(below the {min_age / 3600:.1f}h minimum)."
            )
        await self.send_log(guild, message, color=discord.Color.orange() if young else None)

        if young and cfg["suspicious_log_channel_id"]:
            channel = guild.get_channel(cfg["suspicious_log_channel_id"])
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(
                        f"Young account joined: {new_member.mention} (account age "
                        f"{account_age / 3600:.1f}h) via {inviter.mention}'s invite."
                    )
                except (discord.Forbidden, discord.HTTPException) as exc:
                    log.warning("Failed to post young-account notice: %s", exc)

    # ------------------------------------------------------------------
    # Ticket detection + role removal
    # ------------------------------------------------------------------
    def is_ticket_channel(self, channel: discord.abc.GuildChannel) -> bool:
        cfg = self.get_guild_config(channel.guild.id)
        prefix = cfg["ticket_channel_prefix"]
        category_id = cfg["ticket_category_id"]

        name_match = channel.name.startswith(prefix) if prefix else False

        # A category with its own ping role (/categoryping) is a ticket
        # category in its own right; only the name prefix, if the detection
        # mode uses one, still applies.
        channel_category_id = getattr(channel, "category_id", None)
        if channel_category_id and str(channel_category_id) in cfg["ticket_category_ping_roles"]:
            if cfg["ticket_detection_mode"] == "category" or not prefix:
                return True
            return name_match

        category_match = category_id != 0 and channel_category_id == category_id

        if cfg["ticket_detection_mode"] == "prefix":
            return name_match
        if cfg["ticket_detection_mode"] == "category":
            return category_match
        # "and" (default): require both configured criteria; fall back to
        # whichever single criterion is configured if only one is set.
        if prefix and category_id:
            return name_match and category_match
        return name_match or category_match

    _TOPIC_OPENER_RE = re.compile(r"<@!?(?P<id>\d+)>")

    def _infer_opener_from_overwrites(self, channel: discord.TextChannel) -> Optional[discord.Member]:
        """A private ticket channel has to grant its opener access via a
        per-member permission overwrite -- that's how Ticket Tool (and
        virtually every other ticket bot) scopes the channel to one person.
        If the ticket bot also grants individual overwrites to specific staff
        members (rather than via a role), we can't tell them apart from the
        opener and bail out rather than guess."""
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
        """Determine who opened a ticket. Tried in order, most to least
        reliable: stored mapping, per-member permission overwrites, a user
        mention in the channel topic, then (last resort) a single
        referral-role holder present in the channel."""
        guild = channel.guild

        stored_id = self.ticket_openers.get(str(channel.id))
        if stored_id:
            member = guild.get_member(int(stored_id))
            if member:
                return member

        inferred = self._infer_opener_from_overwrites(channel)
        if inferred:
            self.ticket_openers[str(channel.id)] = str(inferred.id)
            self.save_ticket_openers()
            return inferred

        if channel.topic:
            match = self._TOPIC_OPENER_RE.search(channel.topic)
            if match:
                member = guild.get_member(int(match.group("id")))
                if member:
                    self.ticket_openers[str(channel.id)] = str(member.id)
                    self.save_ticket_openers()
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
        if not isinstance(channel, discord.TextChannel):
            return
        if not self.is_ticket_channel(channel):
            log.info(
                "New text channel #%s (%s) in guild %s did not match ticket detection; ignoring.",
                channel.name, channel.id, channel.guild.id,
            )
            return
        log.info("Detected new ticket channel #%s (%s) in guild %s.", channel.name, channel.id, channel.guild.id)

        # Populate the opener map the moment a ticket channel appears --
        # Ticket Tool sets the per-member overwrite atomically at creation.
        inferred = self._infer_opener_from_overwrites(channel)
        if inferred:
            self.ticket_openers[str(channel.id)] = str(inferred.id)
            self.save_ticket_openers()
        elif channel.topic:
            match = self._TOPIC_OPENER_RE.search(channel.topic)
            if match:
                self.ticket_openers[str(channel.id)] = match.group("id")
                self.save_ticket_openers()

        await self._notify_ticket_ping_role(channel)

    def ticket_ping_role_id(self, channel: discord.TextChannel) -> int:
        """The role to ping for a new ticket: the role mapped to the channel's
        category via /categoryping, else /pingedrole, else the Chef role."""
        cfg = self.get_guild_config(channel.guild.id)
        category_role_id = cfg["ticket_category_ping_roles"].get(str(channel.category_id))
        return category_role_id or cfg["ticket_ping_role_id"] or cfg["chef_role_id"]

    async def _notify_ticket_ping_role(self, channel: discord.TextChannel):
        ping_role_id = self.ticket_ping_role_id(channel)
        if not ping_role_id:
            log.info(
                "No ticket ping role configured for guild %s; skipping notification for #%s.",
                channel.guild.id, channel.name,
            )
            return
        role = channel.guild.get_role(ping_role_id)
        if role is None:
            log.warning(
                "ticket ping role %s not found in guild %s; cannot notify of new ticket %s.",
                ping_role_id, channel.guild.id, channel.id,
            )
            return

        await asyncio.sleep(2)
        for attempt in range(1, 4):
            try:
                await channel.send(
                    f"{role.mention} a new ticket has been opened.",
                    allowed_mentions=discord.AllowedMentions(roles=[role]),
                )
                log.info(
                    "Pinged ticket role %s (%s) in new ticket channel #%s (%d/3).",
                    role.name, role.id, channel.name, attempt,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Failed to notify ticket ping role in new ticket channel %s: %s", channel.id, exc)
                return
            if attempt < 3:
                await asyncio.sleep(3)

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
            return  # already scheduled for this member/ticket

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

    def cancel_pending_removal(self, key: str) -> bool:
        """Cancel one scheduled role removal (used by /cancel_removal)."""
        entry = self.pending_removals.pop(key, None)
        task = self._removal_tasks.pop(key, None)
        if task is not None:
            task.cancel()
        if entry is not None:
            self._save_pending_removals()
        return entry is not None or task is not None

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
            await self.send_log(
                guild, f"🔻 Removed {role.mention} from {member.mention} (ticket handled).",
                color=discord.Color.orange(),
            )
        except discord.Forbidden:
            log.error(
                "Forbidden: cannot remove role %s from %s. "
                "Check the bot's top role is above the referral role and it has Manage Roles.",
                role, member,
            )
        except discord.HTTPException as exc:
            log.error("HTTPException removing role from %s: %s", member, exc)

    # ------------------------------------------------------------------
    # Reminders (restart-safe, same pattern as pending removals)
    # ------------------------------------------------------------------
    def schedule_reminder(self, *, user_id: int, guild_id: int, channel_id: int, remind_at: float, text: str) -> str:
        rid = uuid.uuid4().hex[:8]
        entry = {
            "user_id": user_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "remind_at": remind_at,
            "text": text,
            "created_at": time.time(),
        }
        self.reminders[rid] = entry
        self._save_reminders()
        self._arm_reminder_task(rid, entry)
        return rid

    def cancel_reminder(self, rid: str) -> bool:
        entry = self.reminders.pop(rid, None)
        task = self._reminder_tasks.pop(rid, None)
        if task is not None:
            task.cancel()
        if entry is not None:
            self._save_reminders()
        return entry is not None

    async def _reminder_worker(self, rid: str, entry: dict, delay: float):
        try:
            await asyncio.sleep(delay)
            await self._deliver_reminder(entry)
        finally:
            self.reminders.pop(rid, None)
            self._save_reminders()
            self._reminder_tasks.pop(rid, None)

    async def _deliver_reminder(self, entry: dict):
        embed = discord.Embed(
            title="⏰ Reminder",
            description=entry["text"],
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="Set " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(entry["created_at"])))
        channel = self.get_channel(entry["channel_id"])
        mention = f"<@{entry['user_id']}>"
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            try:
                await channel.send(content=mention, embed=embed)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            user = self.get_user(entry["user_id"]) or await self.fetch_user(entry["user_id"])
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
            log.warning("Could not deliver reminder %s: %s", entry, exc)

    # ------------------------------------------------------------------
    # Warnings
    # ------------------------------------------------------------------
    def add_warning(self, guild_id: int, user_id: int, mod_id: int, reason: str) -> int:
        entries = self.warnings.setdefault(str(guild_id), {}).setdefault(str(user_id), [])
        entries.append({"mod_id": mod_id, "reason": reason, "at": time.time()})
        self.save_warnings()
        return len(entries)

    def get_warnings(self, guild_id: int, user_id: int) -> list:
        return self.warnings.get(str(guild_id), {}).get(str(user_id), [])

    def remove_warning(self, guild_id: int, user_id: int, index: Optional[int] = None) -> Optional[dict]:
        """Remove warning #index (1-based; latest if None). Returns the removed
        entry or None."""
        entries = self.warnings.get(str(guild_id), {}).get(str(user_id), [])
        if not entries:
            return None
        idx = len(entries) - 1 if index is None else index - 1
        if not 0 <= idx < len(entries):
            return None
        removed = entries.pop(idx)
        self.save_warnings()
        return removed

    # ------------------------------------------------------------------
    # Friendly, consistent command error handling
    # ------------------------------------------------------------------
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.HybridCommandError):  # unwrap slash-side errors
            error = error.original
        if isinstance(error, (commands.CommandInvokeError, discord.app_commands.CommandInvokeError)):
            log.error("Unhandled error in command %s: %s", ctx.command, error.original, exc_info=error.original)
            message = "Something went wrong running that command. The error has been logged."
        elif isinstance(error, (commands.MissingPermissions, commands.NotOwner, commands.MissingRole,
                                commands.MissingAnyRole, commands.CheckFailure)):
            message = "You don't have permission to use that command."
        elif isinstance(error, commands.BotMissingPermissions):
            message = "I'm missing the following permission(s) to do that: " + ", ".join(
                f"`{perm}`" for perm in error.missing_permissions
            )
        elif isinstance(error, commands.NoPrivateMessage):
            message = "That command only works inside a server."
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"Slow down — try again in {error.retry_after:.0f}s."
        elif isinstance(error, commands.MissingRequiredArgument):
            message = (
                f"Missing argument: `{error.param.name}`.\n"
                f"Usage: `{ctx.prefix}{ctx.command.qualified_name} {ctx.command.signature}`"
            )
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument, commands.RangeError)):
            message = str(error) or "One of the arguments you gave couldn't be understood."
        else:
            log.error("Unhandled command error in %s: %s", ctx.command, error, exc_info=error)
            message = "Something went wrong running that command. The error has been logged."

        try:
            await ctx.send(embed=error_embed(message), ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
