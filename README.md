# BurgBot 🍔

A multi-purpose `discord.py` bot. Logs who invited whom, pings the right
team when a ticket opens, removes the free meal role once a ticket is
handled, and also does moderation, welcome messages, reminders, polls,
and server info — every command available both as a native `/` slash command
(with Discord's autocomplete picker) and as a classic `!` prefix command.

| Feature | What it does |
|---|---|
| **Invites** | Attributes each join to the invite used and logs "X joined via Y's invite" to the activity log, flagging young accounts. `/who_invited` and `/invited_list` lookups. No scoring or rewards. |
| **Tickets** | Pings a role 3× when a ticket opens: a per-category role (`/categoryping`, one per ticket panel, e.g. Refunds, Uber Eats, Flights) or the default (`/pingedrole`). When a **Chef** replies in a ticket, schedules removal of the free meal role from the opener (restart-safe). Inspect with `/ticket_info` and `/ticketpings`, manage with `/pending_removals` and `/cancel_removal`. |
| **Welcome** | Configurable welcome/goodbye messages with `{mention} {name} {server} {count}` placeholders. |
| **Moderation** | `/purge`, `/kick`, `/ban`, `/unban`, `/timeout`, `/untimeout`, `/slowmode`, persisted warnings (`/warn`, `/warnings`, `/unwarn`). Hierarchy-checked, DM'd to the target, logged. |
| **Utility** | Interactive `/help`, `/ping`, `/botinfo`, `/serverinfo`, `/userinfo`, `/who` (look anyone up by user ID), `/avatar`, restart-safe `/remind` + `/reminders`, `/invites` stats. |
| **Payments** | `/payment` shows the accepted payment methods and whose name each account is under; admins edit the list with `/setpayment` and `/removepayment`. |
| **Fun** | Button polls that survive restarts (`/poll`), `/roll`, `/coinflip`, `/8ball`, `/choose`. |
| **Setup UX** | 4-step dropdown wizard (`/setup`), per-guild config with instant effect, persistent "Start Setup" button on join, `/settings` overview, activity-log channel feed. |

## Project layout

```
bot.py       entrypoint (python bot.py)
config.py    env-var defaults + per-guild config schema + extension list
storage.py   atomic JSON persistence (data/*.json)
ui.py        shared embeds, progress bars, duration parsing, paginator
core.py      BurgBot class: state, gateway events, business logic, timers
cogs/        commands + interactive UI: invites, tickets, welcome,
             moderation, utility, payments, fun, admin
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
least: **Manage Roles** (free meal role removal), **Manage Server** (read invites),
**View Channels / Read Message History** where tickets live, plus whatever
moderation permissions you want it to actually use (Kick/Ban/Moderate
Members/Manage Messages/Manage Channels).

**Important:** the bot's own top role must sit **above** the free meal role
in Server Settings → Roles, or role changes raise `discord.Forbidden`.

### 4. Configure — `/setup` (admins only)

Env vars (see `.env.example`) only provide *defaults*; the wizard is the real
configuration surface. When the bot joins a server it posts a **Start Setup**
button (persistent across restarts); admins can also run `/setup` any time:

1. **Roles & tickets** — free meal role, Chef role, ticket category, name prefix.
2. **Timing** — role removal delay, young-account flag threshold.
3. **Logging** — young-account alert channel, activity-log channel.
4. **Welcome** — welcome/goodbye channels and message templates.

Saved per guild in `data/guild_config.json`; takes effect immediately.
`/settings` shows the current effective config.

### 5. Ticket ping roles (admins only)

- `/pingedrole [role]` — default role pinged on every new ticket (falls
  back to the Chef role).
- `/categoryping <category> [role]` — for tickets created in that category,
  ping this role instead. Run once per ticket panel (e.g. the Refunds
  panel's category → @Refunds). Omit `role` to clear.
- `/ticketpings` — show the default and every per-category role.

A category given its own role counts as a ticket category even if it isn't
the one picked in `/setup`. If the detection mode uses a name prefix,
channels there still need that prefix.

## Commands

Run `/help` in Discord for the interactive, always-up-to-date version.

- **Invites:** `who_invited <member>`, `invited_list <member>`
- **Tickets:** `pingedrole [role]`, `categoryping <category> [role]`,
  `ticketpings`, `ticket_info [channel]`, `set_ticket_opener`,
  `pending_removals`, `cancel_removal`
- **Welcome:** `test_welcome`
- **Moderation:** `purge`, `kick`, `ban`, `unban`, `timeout`, `untimeout`,
  `slowmode`, `warn`, `warnings`, `unwarn`
- **Utility:** `help`, `ping`, `botinfo`, `serverinfo`, `userinfo`, `who`,
  `avatar`, `remind`, `reminders`, `invites`
- **Payments:** `payment`, and admin-only `setpayment`, `removepayment`
- **Fun:** `poll`, `roll`, `coinflip`, `8ball`, `choose`
- **Admin:** `setup`, `settings`, and prefix-only `!sync` (application owner;
  forces a slash-command re-sync — the tree normally syncs itself on startup
  and skips the API call entirely when commands haven't changed)

Staff commands set `default_member_permissions`, so Discord hides them from
the `/` picker for members who lack the permission — a client-side hint on
top of the server-side checks that actually enforce access.

## Payment methods

`/payment` is public and posts an embed with each method, its handle (in a
code block, so it's tap-to-copy on mobile), and the account holder's name.
Admins edit the list in Discord — no redeploy:

- `/setpayment <method> <handle> <holder>` — adds a method, or updates it
  when the method name already exists (case-insensitive; the stored
  spelling is kept). Max 10 methods.
- `/removepayment <method>` — removes one; the method name autocompletes.

The list lives in each guild's config, seeded from
`DEFAULT_PAYMENT_METHODS` in `config.py`, and every edit is posted to the
activity log. Those seeds are **placeholders** (`$cashtag`, holder `Name`)
— set the real handles and names with `/setpayment` in Discord, so real
payment details never go into version control.

## Invite logging (summary)

Every join that can be attributed to an invite is recorded
(`credited_members.json`, filename kept from the old scoring system) and
posted to the activity-log channel. A rejoin notes the previous inviter.
Accounts younger than `min_account_age_seconds` are flagged in that post
and in the young-account alert channel, if set. Nothing is counted,
ranked, or rewarded.

## Ticket-opener detection (summary)

Tried most- to least-reliable: stored mapping (`/set_ticket_opener` or
auto-captured at channel creation) → per-member permission overwrite (how
Ticket Tool scopes a channel to its opener) → `<@id>` mention in the channel
topic → single free-meal-role holder present in the channel. Ambiguous cases
bail out rather than guess; `/ticket_info` shows what the bot concluded.

## Persistence

JSON under `BOT_DATA_DIR` (default `./data/`), written atomically:
`credited_members` (who invited whom), `ticket_openers`, `pending_removals`,
`guild_config`, `warnings`, `reminders`, `polls`, `synced_commands`
(sync-skip cache). Removals, reminders, and poll timers/buttons are all
re-armed on startup, so restarts don't drop scheduled work.

Files left over from the old invite scoring (`referral_progress`,
`total_referrals`, `pending_credits`, `suspicious_joins`) are no longer
used. On startup, any inviter records in the last two are folded into
`credited_members`. After that, all four can be deleted.

**This directory must live on persistent storage, or every redeploy resets
everyone's data.** On Railway (and most container hosts), a service's
filesystem is ephemeral by default — a fresh deploy gets a brand-new, empty
disk. To avoid losing invite records, config, etc. on every push:

1. Attach a persistent **Volume** to the service, with some mount path (e.g. `/data`).
2. Set `BOT_DATA_DIR` to that exact mount path.
3. Redeploy once.

The bot logs a loud `WARNING` at startup if it finds no guild config or
invite records at all — on a host that's been
configured/used before, that means storage isn't wired up correctly. On a
genuinely first-ever boot, it's expected and can be ignored.

## Known limitations

- **Invite attribution is best-effort:** vanity URLs never appear in
  `guild.invites()`, and two simultaneous joins via different invites can be
  ambiguous — the bot skips logging an inviter rather than guessing.
- **Ticket-opener detection is heuristic** (see above) — not a substitute
  for the ticket bot exposing opener data directly, if yours can.
- Failed Discord API calls (role edits, invite fetches) are logged and
  skipped, not retried.
