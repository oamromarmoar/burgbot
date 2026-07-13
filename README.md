# BurgBot 🍔

A multi-purpose `discord.py` bot. Originally a referral-tracker + ticket
role-remover; now also does moderation, welcome messages, reminders, polls,
and server info — every command available both as a native `/` slash command
(with Discord's autocomplete picker) and as a classic `!` prefix command.

| Feature | What it does |
|---|---|
| **Referrals** | Attributes joins to the invite used, counts per referrer, grants a role at a threshold. Progress bar, paginated leaderboard, `/who_invited` lookups. |
| **Anti-abuse** | One credit per account *ever*, retention window before credit finalizes (leaving early voids it), young accounts flagged into an interactive approve/deny review queue. |
| **Tickets** | When a **Chef** replies in a ticket channel, schedules removal of the referral role from the ticket opener (restart-safe). Inspect with `/ticket_info`, manage with `/pending_removals` and `/cancel_removal`. |
| **Welcome** | Configurable welcome/goodbye messages with `{mention} {name} {server} {count}` placeholders. |
| **Moderation** | `/purge`, `/kick`, `/ban`, `/unban`, `/timeout`, `/untimeout`, `/slowmode`, persisted warnings (`/warn`, `/warnings`, `/unwarn`). Hierarchy-checked, DM'd to the target, logged. |
| **Utility** | Interactive `/help`, `/ping`, `/botinfo`, `/serverinfo`, `/userinfo`, `/avatar`, restart-safe `/remind` + `/reminders`, `/invites` stats. |
| **Fun** | Button polls that survive restarts (`/poll`), `/roll`, `/coinflip`, `/8ball`, `/choose`. |
| **Setup UX** | 4-step dropdown wizard (`/setup`), per-guild config with instant effect, persistent "Start Setup" button on join, `/settings` overview, activity-log channel feed. |

## Project layout

```
bot.py       entrypoint (python bot.py)
config.py    env-var defaults + per-guild config schema + extension list
storage.py   atomic JSON persistence (data/*.json)
ui.py        shared embeds, progress bars, duration parsing, paginator
core.py      BurgBot class: state, gateway events, business logic, timers
cogs/        commands + interactive UI: referrals, tickets, welcome,
             moderation, utility, fun, admin
```

To drop a whole feature area, remove its line from `EXTENSIONS` in
`config.py`.

## Setup

### 1. Install

```
pip install -r requirements.txt
cp .env.example .env   # then put your token in it
python bot.py
```

### 2. Developer Portal

Under your application → **Bot**, enable the privileged intents:

- **Server Members Intent** — joins/leaves, roles, welcome messages.
- **Message Content Intent** — `!` prefix commands.

### 3. Invite URL & permissions

Use **both** the `bot` and `applications.commands` OAuth2 scopes. Grant at
least: **Manage Roles** (referral role), **Manage Server** (read invites),
**View Channels / Read Message History** where tickets live, plus whatever
moderation permissions you want it to actually use (Kick/Ban/Moderate
Members/Manage Messages/Manage Channels).

**Important:** the bot's own top role must sit **above** the referral role
in Server Settings → Roles, or role changes raise `discord.Forbidden`.

### 4. Configure — `/setup` (admins only)

Env vars (see `.env.example`) only provide *defaults*; the wizard is the real
configuration surface. When the bot joins a server it posts a **Start Setup**
button (persistent across restarts); admins can also run `/setup` any time:

1. **Roles & tickets** — referral role, Chef role, ticket category, name prefix.
2. **Thresholds & timing** — referrals needed, removal delay, min account age, retention window.
3. **Logging** — suspicious-join alert channel, activity-log channel.
4. **Welcome** — welcome/goodbye channels and message templates.

Saved per guild in `data/guild_config.json`; takes effect immediately.
`/settings` shows the current effective config.

## Commands

Run `/help` in Discord for the interactive, always-up-to-date version.

- **Referrals:** `referrals [member]`, `leaderboard`, `who_invited <member>`,
  `suspicious_joins` (approve/deny buttons), `approve_referral`,
  `deny_referral`, `add_referral`, `remove_referral`
- **Tickets:** `ticket_info [channel]`, `set_ticket_opener`,
  `pending_removals`, `cancel_removal`
- **Welcome:** `test_welcome`
- **Moderation:** `purge`, `kick`, `ban`, `unban`, `timeout`, `untimeout`,
  `slowmode`, `warn`, `warnings`, `unwarn`
- **Utility:** `help`, `ping`, `botinfo`, `serverinfo`, `userinfo`, `avatar`,
  `remind`, `reminders`, `invites`
- **Fun:** `poll`, `roll`, `coinflip`, `8ball`, `choose`
- **Admin:** `setup`, `settings`, and prefix-only `!sync` (application owner;
  forces a slash-command re-sync — the tree normally syncs itself on startup
  and skips the API call entirely when commands haven't changed)

Staff commands set `default_member_permissions`, so Discord hides them from
the `/` picker for members who lack the permission — a client-side hint on
top of the server-side checks that actually enforce access.

## Anti-abuse model (summary)

1. **One credit per Discord account, ever** (`credited_members.json`) —
   defeats leave/rejoin farming.
2. **Retention window** — a credit only finalizes after
   `min_member_retention_seconds` if the member is still present; leaving
   early voids it.
3. **Account-age gate** — accounts younger than `min_account_age_seconds`
   go to the `/suspicious_joins` review queue instead of auto-crediting,
   because a bot can't distinguish "new to Discord" from "farmed alt";
   a human decides.

A determined multi-accounter with aged accounts can still get through — a
bot has no IPs/fingerprints/payment signals. Raise the server's Verification
Level (Discord-side) and consider a phone/captcha verification bot if that
becomes a real problem.

## Ticket-opener detection (summary)

Tried most- to least-reliable: stored mapping (`/set_ticket_opener` or
auto-captured at channel creation) → per-member permission overwrite (how
Ticket Tool scopes a channel to its opener) → `<@id>` mention in the channel
topic → single referral-role holder present in the channel. Ambiguous cases
bail out rather than guess; `/ticket_info` shows what the bot concluded.

## Persistence

JSON under `BOT_DATA_DIR` (default `./data/`), written atomically:
`referral_progress`, `total_referrals` (both `{guild_id: {user_id: count}}`;
old flat single-guild files are auto-migrated on startup),
`credited_members`, `pending_credits`, `suspicious_joins`, `ticket_openers`,
`pending_removals`, `guild_config`, `warnings`, `reminders`, `polls`,
`synced_commands` (sync-skip cache). Pending credits, removals, reminders,
and poll timers/buttons are all re-armed on startup, so restarts don't drop
scheduled work.

**This directory must live on persistent storage, or every redeploy resets
everyone's data.** On Railway (and most container hosts), a service's
filesystem is ephemeral by default — a fresh deploy gets a brand-new, empty
disk. To avoid losing referral counts, config, etc. on every push:

1. Attach a persistent **Volume** to the service, with some mount path (e.g. `/data`).
2. Set `BOT_DATA_DIR` to that exact mount path.
3. Redeploy once.

The bot logs a loud `WARNING` at startup if it finds no guild config,
credited members, or referral totals at all — on a host that's been
configured/used before, that means storage isn't wired up correctly. On a
genuinely first-ever boot, it's expected and can be ignored.

## Known limitations

- **Invite attribution is best-effort:** vanity URLs never appear in
  `guild.invites()`, and two simultaneous joins via different invites can be
  ambiguous — the bot skips crediting rather than guessing.
- **Ticket-opener detection is heuristic** (see above) — not a substitute
  for the ticket bot exposing opener data directly, if yours can.
- Failed Discord API calls (role edits, invite fetches) are logged and
  skipped, not retried.
