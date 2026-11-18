"""
Member backup. Members press a Verify button and authorize the bot through
Discord's own OAuth2 page ("join servers for you"). The bot keeps their
tokens so that, if this server is ever lost, the bot owner can run /restore
in a backup server and add everyone who verified.

Only members who verified can be restored -- Discord has no way to add
anyone who didn't. Members can revoke at any time (User Settings ->
Authorized Apps), and leaving the server drops their record.

Needs a small web endpoint for Discord to send people back to after they
authorize. Configure with env vars:
  DISCORD_CLIENT_SECRET  Developer Portal -> OAuth2 -> Client Secret
  PUBLIC_URL             the bot's public https URL (on Railway, a generated
                         domain is picked up automatically)
  PORT                   the port to listen on (Railway sets this)
and add PUBLIC_URL + "/callback" under Developer Portal -> OAuth2 -> Redirects.
"""

import asyncio
import html
import logging
import secrets
import time
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from config import DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, PUBLIC_URL, WEB_PORT
from storage import BACKUP_MEMBERS_FILE, load_json, save_json
from ui import error_embed, info_embed, ok_embed, warn_embed

if TYPE_CHECKING:
    from core import BurgBot

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
SCOPES = "identify guilds.join"
STATE_SECONDS = 10 * 60          # how long a Verify link stays usable
RESTORE_PAUSE_SECONDS = 1.0      # between member adds, to stay well inside rate limits
PROGRESS_EVERY = 25

# Discord JSON error codes seen when adding members.
ERR_MISSING_PERMISSIONS = 50013
ERR_INVALID_OAUTH_TOKEN = 50025
ERR_TOO_MANY_GUILDS = 30001

SETUP_HELP = (
    "Member backup isn't set up yet. On Railway, set `DISCORD_CLIENT_SECRET` (Developer Portal → OAuth2) "
    "and generate a public domain for the service (or set `PUBLIC_URL`), then add "
    "`<that URL>/callback` under Developer Portal → OAuth2 → Redirects and redeploy."
)


class OAuthError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body if isinstance(body, dict) else {}


def _page(title: str, message: str, status: int = 200) -> web.Response:
    """A minimal page shown in the member's browser after authorizing."""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#1e1f22; color:#f2f3f5;
         font-family:system-ui,-apple-system,"Segoe UI",sans-serif; padding:16px; box-sizing:border-box; }}
  main {{ max-width:420px; background:#2b2d31; border-radius:12px; padding:28px; text-align:center; }}
  h1 {{ font-size:1.4rem; margin:0 0 12px; }}
  p {{ color:#b5bac1; line-height:1.5; margin:0; }}
</style></head>
<body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></main></body></html>"""
    return web.Response(text=body, content_type="text/html", status=status)


# ----------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------
class VerifyView(discord.ui.View):
    """The Verify panel. Persistent: fixed custom_id + timeout=None +
    add_view() in cog_load keep the button working after restarts."""

    def __init__(self, cog: "Backup"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, emoji="✅",
                       custom_id="burgbot:backup_verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_verify(interaction)


class ConfirmRestoreView(discord.ui.View):
    def __init__(self, cog: "Backup", author_id: int, channel: discord.abc.Messageable):
        super().__init__(timeout=60)
        self.cog, self.author_id, self.channel = cog, author_id, channel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Start restore", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        if self.cog.restore_running:
            await interaction.response.edit_message(embed=error_embed("A restore is already running."), view=None)
            return
        await interaction.response.edit_message(embed=info_embed("Starting the restore…"), view=None)
        self.cog.start_restore(interaction.guild, self.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(embed=info_embed("Restore cancelled."), view=None)


# ----------------------------------------------------------------------
# Cog
# ----------------------------------------------------------------------
class Backup(commands.Cog):
    """Member backup: a Verify button now, /restore into a backup server later."""

    def __init__(self, bot: "BurgBot"):
        self.bot = bot
        # user_id (str) -> {"access_token", "refresh_token", "expires_at",
        #                   "username", "guild_id", "verified_at"}
        self.members: dict = load_json(BACKUP_MEMBERS_FILE, {})
        # OAuth state -> {"user_id", "guild_id", "expires"}; in memory only.
        self._states: dict = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None
        self._restore_task: Optional[asyncio.Task] = None

    # --- lifecycle ----------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(DISCORD_CLIENT_SECRET and PUBLIC_URL)

    @property
    def redirect_uri(self) -> str:
        return f"{PUBLIC_URL}/callback"

    @property
    def client_id(self) -> int:
        return DISCORD_CLIENT_ID or self.bot.application_id

    @property
    def restore_running(self) -> bool:
        return self._restore_task is not None and not self._restore_task.done()

    async def cog_load(self):
        self.bot.add_view(VerifyView(self))
        self._session = aiohttp.ClientSession()
        if not self.configured:
            log.warning("Member backup disabled: set DISCORD_CLIENT_SECRET and PUBLIC_URL to enable it.")
            return
        app = web.Application()
        app.router.add_get("/", self._health)
        app.router.add_get("/callback", self._callback)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", WEB_PORT).start()
        log.info("Member backup web endpoint listening on port %d (%s).", WEB_PORT, self.redirect_uri)

    async def cog_unload(self):
        if self._restore_task is not None:
            self._restore_task.cancel()
        if self._runner is not None:
            await self._runner.cleanup()
        if self._session is not None:
            await self._session.close()

    def _save(self):
        save_json(BACKUP_MEMBERS_FILE, self.members)

    # --- OAuth helpers ------------------------------------------------
    async def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": str(self.client_id), "client_secret": DISCORD_CLIENT_SECRET}
        async with self._session.post(f"{API}/oauth2/token", data=data) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise OAuthError(resp.status, body)
            return body

    async def _fetch_user(self, access_token: str) -> dict:
        async with self._session.get(f"{API}/users/@me",
                                     headers={"Authorization": f"Bearer {access_token}"}) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise OAuthError(resp.status, body)
            return body

    async def _access_token(self, user_id: str, entry: dict) -> Optional[str]:
        """A usable access token for this member, refreshing it if expired.
        None (and the record dropped) if they revoked the authorization."""
        if entry["expires_at"] - 60 > time.time():
            return entry["access_token"]
        try:
            token = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": entry["refresh_token"]}
            )
        except OAuthError as exc:
            if exc.body.get("error") == "invalid_grant":
                self.members.pop(user_id, None)
                self._save()
            else:
                log.warning("Couldn't refresh backup token for %s: %s", user_id, exc)
            return None
        entry.update(
            access_token=token["access_token"],
            refresh_token=token.get("refresh_token", entry["refresh_token"]),
            expires_at=time.time() + token.get("expires_in", 0),
        )
        self._save()
        return entry["access_token"]

    # --- verify flow --------------------------------------------------
    async def start_verify(self, interaction: discord.Interaction):
        if not self.configured:
            await interaction.response.send_message(
                embed=error_embed("Verification isn't set up yet — please let an admin know."), ephemeral=True
            )
            return
        now = time.time()
        for state, entry in list(self._states.items()):
            if entry["expires"] < now:
                del self._states[state]
        state = secrets.token_urlsafe(24)
        self._states[state] = {"user_id": interaction.user.id, "guild_id": interaction.guild_id,
                               "expires": now + STATE_SECONDS}
        url = AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        })
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Authorize with Discord", url=url))
        await interaction.response.send_message(embed=info_embed(
            "Press the button below and approve on Discord's page. This lets the bot add you to our backup "
            "server if this one ever goes down — nothing else.\n\n"
            "You can undo this any time in **User Settings → Authorized Apps**. The link works for 10 minutes.",
            title="✅ Verify",
        ), view=view, ephemeral=True)

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _callback(self, request: web.Request) -> web.Response:
        entry = self._states.pop(request.query.get("state", ""), None)
        if request.query.get("error"):
            return _page("Verification cancelled",
                         "You didn't authorize the bot. You can try again from the Verify button in Discord.", 400)
        code = request.query.get("code")
        if entry is None or entry["expires"] < time.time() or not code:
            return _page("Link expired",
                         "This verification link is invalid or has expired. Press Verify in Discord again.", 400)
        try:
            token = await self._token_request(
                {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri}
            )
            user = await self._fetch_user(token["access_token"])
        except (OAuthError, aiohttp.ClientError, KeyError) as exc:
            log.warning("Backup verification failed: %s", exc)
            return _page("Something went wrong", "Discord didn't accept the verification. Please try again.", 502)

        if "guilds.join" not in token.get("scope", "").split():
            return _page("Permission missing",
                         "The \"join servers for you\" permission is needed. Press Verify in Discord again.", 400)
        if int(user["id"]) != entry["user_id"]:
            return _page("Wrong account",
                         "You signed in with a different Discord account than the one that pressed Verify. "
                         "Switch accounts in your browser and try again.", 400)

        self.members[user["id"]] = {
            "access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "expires_at": time.time() + token.get("expires_in", 0),
            "username": user.get("username"),
            "guild_id": entry["guild_id"],
            "verified_at": time.time(),
        }
        self._save()
        await self._grant_role(entry["guild_id"], entry["user_id"])
        return _page("You're verified ✅", "All set — you can close this tab and head back to Discord.")

    async def _grant_role(self, guild_id: Optional[int], user_id: int):
        guild = self.bot.get_guild(guild_id) if guild_id else None
        if guild is None:
            return
        role = guild.get_role(self.bot.get_guild_config(guild.id)["verify_role_id"])
        member = guild.get_member(user_id)
        if role is None or member is None or role in member.roles:
            return
        try:
            await member.add_roles(role, reason="Verified for member backup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await self.bot.send_log(
                guild, f"⚠️ {member.mention} verified, but I couldn't give them {role.mention} ({exc}). "
                       "Move my role above it and make sure I have **Manage Roles**."
            )

    # --- restore ------------------------------------------------------
    def start_restore(self, guild: discord.Guild, channel: discord.abc.Messageable, by: discord.abc.User):
        self._restore_task = asyncio.create_task(self._run_restore(guild, channel, by))

    async def _run_restore(self, guild: discord.Guild, channel: discord.abc.Messageable, by: discord.abc.User):
        entries = list(self.members.items())
        counts = {"added": 0, "already": 0, "revoked": 0, "full": 0, "failed": 0}

        def summary(done: int) -> str:
            return (f"**{done}/{len(entries)}** processed\n"
                    f"✅ Added: **{counts['added']}**\n"
                    f"👥 Already here: **{counts['already']}**\n"
                    f"🚫 Revoked authorization: **{counts['revoked']}**\n"
                    f"📛 In too many servers: **{counts['full']}**\n"
                    f"⚠️ Other errors: **{counts['failed']}**")

        progress = await channel.send(embed=info_embed(summary(0), title=f"🛟 Restoring into {guild.name}…"))

        async def update(title: str, done: int):
            try:
                await progress.edit(embed=info_embed(summary(done), title=title))
            except (discord.NotFound, discord.HTTPException):
                pass

        for index, (user_id, entry) in enumerate(entries, 1):
            if guild.get_member(int(user_id)) is not None:
                counts["already"] += 1
                continue
            access_token = await self._access_token(user_id, entry)
            if access_token is None:
                counts["revoked"] += 1
                continue
            route = discord.http.Route("PUT", "/guilds/{guild_id}/members/{user_id}",
                                       guild_id=guild.id, user_id=int(user_id))
            try:
                added = await self.bot.http.request(route, json={"access_token": access_token})
                counts["added" if added else "already"] += 1  # 201 returns the member, 204 nothing
            except discord.HTTPException as exc:
                if exc.code == ERR_MISSING_PERMISSIONS:
                    await update(f"🛟 Restore stopped — I need **Create Invite** in {guild.name}", index - 1)
                    return
                if exc.code == ERR_INVALID_OAUTH_TOKEN:
                    self.members.pop(user_id, None)
                    self._save()
                    counts["revoked"] += 1
                elif exc.code == ERR_TOO_MANY_GUILDS:
                    counts["full"] += 1
                else:
                    log.warning("Restore: couldn't add %s to %s: %s", user_id, guild.id, exc)
                    counts["failed"] += 1
            if index % PROGRESS_EVERY == 0:
                await update(f"🛟 Restoring into {guild.name}…", index)
            await asyncio.sleep(RESTORE_PAUSE_SECONDS)

        await update(f"🛟 Restore into {guild.name} finished", len(entries))
        await self.bot.send_log(
            guild, f"🛟 {by.mention} restored members into this server: {counts['added']} added, "
                   f"{counts['already']} already here, {counts['revoked']} revoked."
        )

    # --- events -------------------------------------------------------
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Someone who leaves (or is kicked/banned from) the server they
        verified in shouldn't be pulled into the backup later."""
        entry = self.members.get(str(member.id))
        if entry is not None and entry.get("guild_id") == member.guild.id:
            del self.members[str(member.id)]
            self._save()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """The bot lost a server (it was deleted, or the bot was kicked).
        Tell the owner, since the restore is theirs to start."""
        saved = sum(1 for e in self.members.values() if e.get("guild_id") == guild.id)
        if not saved:
            return
        try:
            app = await self.bot.application_info()
            owner = app.team.owner if app.team else app.owner
            if owner is not None:
                await owner.send(embed=warn_embed(
                    f"I'm no longer in **{guild.name}** — it was deleted, or I was removed. "
                    f"**{saved}** verified member(s) from it are saved. If you need them, add me to your backup "
                    f"server and run `/restore` there."
                ))
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Couldn't DM the owner about leaving guild %s: %s", guild.id, exc)

    # --- commands -----------------------------------------------------
    @commands.hybrid_command(name="verifypanel", description="Post the Verify button for member backup here.")
    @app_commands.describe(role="Optional role to give members once they verify.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def verifypanel(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """Post the Verify panel in this channel. Members who verify can be
        added to a backup server later with /restore. Requires Administrator."""
        if not self.configured:
            await ctx.send(embed=error_embed(SETUP_HELP), ephemeral=True)
            return
        if role is not None:
            if role >= ctx.guild.me.top_role or role.managed:
                await ctx.send(embed=error_embed(
                    f"I can't give out {role.mention} — move my role above it first."
                ), ephemeral=True)
                return
            self.bot.update_guild_config(ctx.guild.id, verify_role_id=role.id)
        embed = discord.Embed(
            title="✅ Verify",
            description=(
                "Press **Verify** to back up your membership. If this server ever goes down, you'll be "
                "added to our backup server automatically, so you don't lose us.\n\n"
                "It uses Discord's own authorization page, and you can undo it any time in "
                "**User Settings → Authorized Apps**."
            ),
            color=discord.Color.green(),
        )
        await ctx.channel.send(embed=embed, view=VerifyView(self))
        if ctx.interaction is not None:
            await ctx.send(embed=ok_embed("Verify panel posted."), ephemeral=True)
        await self.bot.send_log(ctx.guild, f"🛟 {ctx.author.mention} posted the Verify panel in {ctx.channel.mention}.")

    @commands.hybrid_command(name="backupstats", description="How many members have verified for member backup.")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def backupstats(self, ctx: commands.Context):
        """Show how many members are saved for /restore."""
        here = sum(1 for e in self.members.values() if e.get("guild_id") == ctx.guild.id)
        humans = sum(1 for m in ctx.guild.members if not m.bot)
        percent = f" ({here / humans:.0%} of members)" if humans else ""
        await ctx.send(embed=info_embed(
            f"**{here}** member(s) verified in this server{percent}.\n"
            f"**{len(self.members)}** saved in total across all servers.",
            title="🛟 Member backup",
        ), ephemeral=True)

    @commands.hybrid_command(name="restore", description="Add every verified member to this server (bot owner only).")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    @commands.guild_only()
    async def restore(self, ctx: commands.Context):
        """Run in the backup server: adds every member who verified to it.
        Bot owner only, so no other server the bot is in can pull members."""
        if not self.configured:
            await ctx.send(embed=error_embed(SETUP_HELP), ephemeral=True)
            return
        if self.restore_running:
            await ctx.send(embed=error_embed("A restore is already running."), ephemeral=True)
            return
        if not ctx.guild.me.guild_permissions.create_instant_invite:
            await ctx.send(embed=error_embed("I need the **Create Invite** permission here to add members."),
                           ephemeral=True)
            return
        if not self.members:
            await ctx.send(embed=error_embed("No verified members are saved."), ephemeral=True)
            return
        minutes = max(1, round(len(self.members) * RESTORE_PAUSE_SECONDS / 60))
        await ctx.send(embed=warn_embed(
            f"Add all **{len(self.members)}** verified member(s) to **{ctx.guild.name}**? "
            f"This takes about {minutes} minute(s); progress is posted in this channel."
        ), view=ConfirmRestoreView(self, ctx.author.id, ctx.channel), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Backup(bot))
