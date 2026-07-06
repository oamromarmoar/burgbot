# Discord Referral + Ticket Role-Removal Bot

A single `discord.py` bot with two independent features:

1. **Referral tracking** — attributes new-member joins to the invite link
   they used, counts referrals per member, and grants a role once a
   configurable threshold is reached. Tracks two numbers per referrer:
   progress toward their *next* reward (resets to 0 once the role is
   removed) and a lifetime total (never resets).
2. **Ticket role removal** — when a "Chef" replies in a ticket channel,
   schedules removal of the referral role from the ticket opener after a
   configurable delay, and resets that member's progress toward their next
   reward. Survives bot restarts.

## Setup

### 1. Install dependencies

```
pip install -r requirements.txt
```

### 2. Developer Portal configuration

In the [Discord Developer Portal](https://discord.com/developers/applications),
under your application -> **Bot**:

- Enable **Server Members Intent** (privileged) — required for `on_member_join`
  and for resolving members/roles reliably.
- Enable **Message Content Intent** (privileged) — required by the intents
  requested in code (`intents.message_content = True`).
- The **invites** intent (`intents.invites`) is *not* privileged and needs no
  toggle in the portal.

### 3. Bot permissions

When generating the OAuth2 invite URL, grant at least:

- **Manage Roles** — to add/remove the referral role.
- **Manage Server** — required to call `guild.invites()` and see invite use
  counts.
- **View Channels** / **Read Message History** in whatever channels/categories
  contain tickets.

**Important:** the bot's own top role must be positioned **above** the
referral role in Server Settings -> Roles, or role add/remove calls will
raise `discord.Forbidden` regardless of the Manage Roles permission.

### 4. Configure

There are two layers of configuration:

1. **Env vars** (`.env` / `DISCORD_TOKEN` etc.) — set the *default* values
   used until a server configures itself, and always hold `DISCORD_TOKEN`,
   `BOT_DATA_DIR`, and `COMMAND_PREFIX` (these three aren't per-guild).
2. **The setup wizard** (see below) — lets each server's admins configure
   everything else (roles, ticket detection, thresholds, timing) through
   Discord's UI instead of editing env vars. Per-guild settings are stored in
   `data/guild_config.json` and override the env-var defaults for that guild
   only, immediately, with no restart.

Copy `.env.example` to `.env` (or otherwise export the same variables) and
fill in at least the token:

```
cp .env.example .env
```

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Bot token (required, no default). |
| `REFERRAL_ROLE_ID` | Default: role granted after enough referrals, and removed after a handled ticket. |
| `REFERRALS_NEEDED` | Default: referral count required to grant the role. |
| `MIN_ACCOUNT_AGE_SECONDS` | Default: below this Discord account age, a join is queued for manual review instead of auto-credited (259200 = 3 days). |
| `MIN_MEMBER_RETENTION_SECONDS` | Default: a credited join is held this long before it counts; leaving early voids it (86400 = 24h). |
| `SUSPICIOUS_LOG_CHANNEL_ID` | Default: optional channel to post suspicious-join alerts to (0 = disabled, still visible via `!suspicious_joins`). |
| `CHEF_ROLE_ID` | Default: role that triggers scheduled removal when its holder posts in a ticket. |
| `TICKET_DETECTION_MODE` | Default: `prefix`, `category`, or `and` (requires both configured criteria to match — falls back to whichever single one is set if only one is configured). |
| `TICKET_CHANNEL_PREFIX` | Default: ticket channel name prefix, e.g. `ticket-`. |
| `TICKET_CATEGORY_ID` | Default: category ID tickets live under. |
| `ROLE_REMOVAL_DELAY_SECONDS` | Default: delay before removal after a Chef message (1200 = 20 min). |
| `BOT_DATA_DIR` | Where all JSON data files live (default `./data`). Not per-guild. |
| `COMMAND_PREFIX` | Command prefix (default `!`). Not per-guild. |

### 5. Setup wizard (per-server, admins only)

When the bot joins a server, it posts a "Start Setup" prompt in the system
channel (or the first channel it can post in). Any time after that, an admin
can also run `!setup` to (re)configure. Both are gated to members with the
**Administrator** permission — non-admins get a rejection message if they
try to use the command or click the wizard's buttons/dropdowns.

The wizard is a 3-step flow using Discord's native pickers:

1. **Referral role**, **Chef role** (role dropdowns), **ticket category**
   (channel dropdown), and ticket name prefix (via a button that opens a
   short text-input modal).
2. **Referrals needed**, **role removal delay**, **minimum account age**,
   **minimum member retention** — all preset dropdowns, no typing required.
3. **Suspicious-join log channel** (optional), then **Save Configuration**.

Changes take effect immediately on save — no restart needed. If the bot
restarts before an admin clicks the on-join "Start Setup" button, that
specific button stops working (Discord interactions on old messages don't
survive a process restart unless a bot re-registers persistent views, which
this one doesn't) — `!setup` is unaffected and always works as the reliable
way to configure or reconfigure a server.

If you'd rather use a `.env` file with `python-dotenv`, add
`from dotenv import load_dotenv; load_dotenv()` at the top of `bot.py` and
`pip install python-dotenv` — omitted here to keep the dependency list
minimal.

### 5. Run

```
python bot.py
```

## Commands

- `!setup` — (re)configure this server via the dropdown wizard. Requires
  Administrator.
- `!referrals_count [member]` — reports a member's referral total (defaults
  to yourself).
- `!set_ticket_opener <channel> <member>` — manually records who opened a
  ticket, for cases the automatic detection can't figure out. Requires
  Manage Roles.
- `!suspicious_joins` — lists joins flagged for account age, awaiting review.
  Requires Manage Roles.
- `!approve_referral <member>` — manually credits a flagged join (staff
  confirmed it's legitimate). Requires Manage Roles.
- `!deny_referral <member>` — discards a flagged join without crediting
  anyone. Requires Manage Roles.

## Anti-abuse (leave/rejoin farming and alt-account self-referrals)

Referral counting is deliberately **not** immediate — every credited join
goes through two gates before it's permanent:

1. **One credit per Discord account, ever.** `credited_members.json` records
   every member ID that has ever been credited, to whoever referred them.
   Once present, that account can never generate another credit — not by
   leaving and rejoining on the same invite, not by rejoining on a different
   referrer's invite. This alone defeats simple leave/rejoin farming.

2. **Retention window before a credit finalizes.** A qualifying join is
   written to `pending_credits.json` and only turned into an actual credit
   after `MIN_MEMBER_RETENTION_SECONDS` (default 24h) — and only if the
   member is still in the guild at that point. `on_member_remove` cancels
   the pending credit the moment the member leaves early. This is what
   defeats "join a batch of alts, get instant credit, immediately leave" —
   each alt now has to occupy a seat in the server for the full window,
   which is far more visible and costly for an abuser than a drive-by join.

3. **Account-age gate for likely alts.** If the joining account is younger
   than `MIN_ACCOUNT_AGE_SECONDS` (default 3 days), it's never
   auto-credited — it's written to `suspicious_joins.json` for staff to
   review with `!suspicious_joins` / `!approve_referral` / `!deny_referral`.
   Joins aren't silently rejected here because a brand-new Discord account is
   also just... a new Discord user; a bot can't tell the difference between
   that and a farmed alt, so this defers the judgment call to a human instead
   of guessing wrong in either direction.

### Hard limit: this cannot fully stop determined multi-accounting

A bot has no access to IP addresses, device fingerprints, or payment info, so
there's no way to prove two Discord accounts belong to the same person.
Someone willing to age accounts for a few days, keep them in the server for
24h, and use different invites can still get through. To raise the bar
further:

- **Raise the server's Verification Level** (Server Settings -> Safety
  Setup -> Verification Level) to at least **Medium** (registered on Discord
  >5 min) or **High** (member of *some* server >10 min) — this is a
  Discord-side setting, not something this bot configures, and stacks with
  the account-age gate above.
- Consider a dedicated verification bot (phone/captcha-based) for new
  members if self-referral abuse becomes a real problem — that's the class
  of signal (phone number, payment method) that can actually link accounts,
  and is out of scope for what a bot without those integrations can do.
- If needed, add a per-referrer daily/weekly cap on auto-credits as a
  circuit breaker (not implemented here, but a small addition to
  `_attribute_join` if bulk alt farming becomes a pattern you're seeing).

## How ticket-opener detection works (and its tradeoffs)

The bot doesn't control the separate ticket bot, so it can't know the opener
with certainty. It tries, in order:

1. **Stored mapping** (`ticket_openers.json`) — most reliable, and normally
   already populated by the time it's needed, because `on_guild_channel_create`
   fills it in via method 2 the moment the ticket channel appears. Can also be
   set manually via `!set_ticket_opener`.
2. **Per-member permission overwrites** — the only way Discord lets a bot
   scope a channel to one specific non-role-based user is a per-member
   permission overwrite, and that's exactly how Ticket Tool (and effectively
   every other ticket bot) grants the opener access to their private channel.
   Reading that overwrite identifies who the channel was created for,
   independent of what roles they currently hold — unlike method 4 below, this
   can't be fooled by an unrelated referral-role holder who happens to have
   channel access. Tradeoff: if the ticket bot grants *specific staff members*
   individual overwrites too (rather than via a role), there's no way to tell
   them apart from the opener, so this bails out rather than guess if more
   than one non-staff member has a personal overwrite.
3. **Channel topic** — looks for a `<@user_id>` mention in the topic, in case
   the ticket bot is configured to include one. Fragile: entirely dependent
   on that specific bot/config, and fails silently if absent.
4. **Last-resort fallback** — looks for a member present in the channel who
   holds `REFERRAL_ROLE_ID`, and only acts if exactly one such member is
   found. This is the weakest signal: it assumes the opener is the only
   referral-role holder with access to the channel; if staff also hold that
   role, or a past referrer can still see the channel, this either picks the
   wrong person or (safely) gives up rather than guess. Only reached if
   methods 1-3 all come up empty.

## Known limitations

- **Invite attribution is best-effort.** Vanity URLs never appear in
  `guild.invites()` and have no use counter, so joins via a vanity URL can't
  be attributed to anyone. Simultaneous invite uses (two people joining via
  different invites in the same instant, or a race between the join event and
  the invite-count update) can also make attribution ambiguous; the bot skips
  crediting rather than guessing when it can't find a unique invite whose use
  count increased.
- **Ticket opener detection is heuristic**, as described above — it is not a
  substitute for the ticket bot exposing this data directly (e.g. via a
  webhook or shared database), if that's an option.
- Both `discord.Forbidden` and `discord.HTTPException` are caught around all
  invite fetches and role edits and logged, but the underlying action (grant,
  attribute, or remove) is simply skipped when they occur — it is not
  retried automatically.

## Persistence model

JSON files under `BOT_DATA_DIR` (default `./data/`):

- `referral_progress.json` — `{ "<user_id>": <count> }` — progress toward the next reward; reset to 0 when the referral role is removed.
- `total_referrals.json` — `{ "<user_id>": <count> }` — lifetime total, never reset.
- `ticket_openers.json` — `{ "<channel_id>": "<user_id>" }`
- `pending_removals.json` — `{ "<channel_id>:<user_id>": { "guild_id", "channel_id", "member_id", "remove_at" } }`
- `credited_members.json` — `{ "<member_id>": "<referrer_id>" }` — permanent, never re-credited once present.
- `pending_credits.json` — `{ "<member_id>": { "guild_id", "member_id", "referrer_id", "credit_at" } }`
- `suspicious_joins.json` — `{ "<member_id>": { "guild_id", "member_id", "referrer_id", "account_age_seconds", "flagged_at" } }`
- `guild_config.json` — `{ "<guild_id>": { ...whatever fields that guild's admins have set via !setup... } }`. Anything not present here falls back to the env-var default for that field (see `DEFAULT_GUILD_CONFIG` / `get_guild_config()` in `bot.py`).

`pending_removals.json` is the important one for restart-safety: every
scheduled removal is written to disk immediately when scheduled, and removed
once it fires. On startup, `setup_hook()` re-reads this file and re-arms an
`asyncio` task for each entry with whatever time remains (or fires it
immediately if the delay already elapsed while the bot was offline).
