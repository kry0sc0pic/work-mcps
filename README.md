# work-mcps

MCP servers for the **candidate side** of applicant tracking systems (ATS): check where your job
applications stand without clicking through every employer's careers portal.

## Setup

Requires [uv](https://docs.astral.sh/uv/) on macOS, Windows or Linux (see [Platform support](#platform-support)).

```sh
git clone https://github.com/kry0sc0pic/work-mcps.git work-mcps && cd work-mcps
uv sync --all-packages
uv run work-mcp setup
```

The wizard walks through:
1. **Platforms:** which ATS platforms to connect (Workday, Oracle Recruiting Cloud, Greenhouse, Work at a Startup).
2. **Logins:** import them from a password-manager CSV (Apple Passwords, Chrome, 1Password,
   Bitwarden), type them in one at a time, or skip.
3. **Registration:** pick which AI clients get the MCP servers (detected ones are pre-ticked; see
   [Supported AI clients](#supported-ai-clients)). Anything it can't do itself, it prints as a command to run.

Passwords go to your system keychain. Only the host, site name and username are written to
`~/.config/work-mcps/workday-accounts.json`. Run the wizard in a normal terminal, since the hidden
password prompt needs a TTY.

## Platform support

Passwords are stored with [`keyring`](https://pypi.org/project/keyring/), which uses your OS keychain:

| OS | Password storage | Password dialog (`workday_add_account`) |
|---|---|---|
| macOS | Keychain | Native dialog |
| Windows | Credential Manager | Tk dialog |
| Linux | Secret Service (GNOME Keyring or KWallet) | Tk dialog (needs `tkinter` and a display) |

- **Linux** needs a desktop session with D-Bus and an unlocked keyring. Headless servers, containers and
  bare WSL usually have none, so saving a login fails with a message saying so. The tool never
  falls back to a file on its own; headless storage is an explicit opt-in below.
- No keychain at all (headless server)? See [Headless Linux](#headless-linux).
- Where there is no graphical dialog, use `uv run mcp-workday-login <url> <email>` in a terminal.
- Config and cache files live in `~/.config/work-mcps` and `~/.cache/work-mcps` (XDG variables are
  honoured), or under `%APPDATA%` and `%LOCALAPPDATA%` on Windows.
- CI runs lint, type checks and tests on all three systems. The author has so far only tried it by
  hand on macOS, so please open an issue if something misbehaves on Windows or Linux.

## Headless Linux

Servers and containers usually have no keychain, so saving a login fails. `work-mcp setup` notices and
offers to fix it; you can also run it yourself:

```sh
uv run work-mcp headless                    # asks: encrypted file or plain-text file
uv run work-mcp headless --mode encrypted
uv run work-mcp headless --mode plaintext   # explicit opt-in, asks to confirm
uv run work-mcp headless --off              # back to the OS keychain
```

| Mode | Where | Protection |
|---|---|---|
| Encrypted file | `~/.config/work-mcps/credentials.enc` | Argon2 + AES. A random key is generated into `keyring.key` (mode 600) on the same machine, so this protects copies and backups of the data file, not someone who can read both files. |
| Plain-text file | `~/.config/work-mcps/credentials.json` | None. Mode 600 only: anyone who can read the file or a backup of it can read your passwords. |

The choice is saved in `keyring.json` next to them, so no environment variables are needed, including
for AI clients that don't pass your environment to servers. Logins saved in another store aren't moved;
re-add or re-import them. These modes exist only on Linux and are never picked automatically.
(`WORK_MCP_KEYRING=file|plaintext` overrides the saved choice.)

## Supported AI clients

The wizard (and `work-mcp register`) adds each server to the clients you pick. Setup is read from each
client's docs; only Claude Code has been run end to end so far, so please report problems.

| Client | How it's registered |
|---|---|
| Claude Code | `claude mcp add --scope user` |
| Codex | `codex mcp add` (writes `~/.codex/config.toml`) |
| Grok Build | `grok mcp add` (writes `~/.grok/config.toml`) |
| OpenClaw | `openclaw mcp set` |
| Cursor | edits `~/.cursor/mcp.json` (`mcpServers`) |
| Hermes Agent | edits `~/.hermes/config.yaml` (`mcp_servers`); comments are kept |

Existing entries named `workday` are never overwritten, and other servers in those files are left alone.
Restart the client (Hermes: `/reload-mcp`) so it picks up the new tools. Missing the CLI or file? The
wizard prints the command or snippet to use by hand.

## Importing passwords

```sh
uv run work-mcp import ~/Downloads/Passwords.csv --dry-run   # preview, saves nothing
uv run work-mcp import ~/Downloads/Passwords.csv             # pick platforms, save, offer to delete the CSV
```

> **Your passwords never leave your machine.** The CSV is read locally and the logins go straight into
> your system keychain. Nothing is uploaded anywhere. The importer's only network request is a public
> `robots.txt` fetch with no credentials, and later logins send your password to the employer's own
> login page over HTTPS, as a browser would.

| Password manager | Status | How to export |
|---|---|---|
| Apple Passwords | ✅ Implemented (checked against a real export) | File → Export All Passwords…; you can export every password at once |
| Chrome | Should work, untested | Settings → Passwords → Export passwords |
| 1Password, Bitwarden | Should work, untested | Export as CSV |

The CSV contains **every** password you own in plain text, not just job-site logins. Only rows for
supported platforms are read, nothing is printed, and the tool offers to delete the file when it's
done. Delete it yourself if you skip that step.

The import shows one checkbox row per platform, for example `Workday: 2 passwords`. Space toggles a
platform, Enter confirms. Any `*.myworkdayjobs.com` URL counts as Workday. Password managers often
save only the host (`https://acme.wd1.myworkdayjobs.com`), so after you confirm, the importer reads
that host's public `robots.txt` to find the careers-site name. If a host lists none, that row is
skipped and you can add it with a full careers URL.

Rows are reported as `new`, `update`, `unchanged`, `conflict` (two different logins for one host) or
`skipped` (for example a URL with no site name, or a row with no saved password).

## Other commands

```sh
uv run work-mcp accounts                 # saved accounts (never passwords)
uv run work-mcp register                 # (re)register the servers with your detected AI clients
uv run work-mcp register -c cursor -c codex   # or name the clients: claude, codex, cursor, grok, hermes, openclaw
uv run mcp-workday-login <url> <email>   # add a single account from a terminal prompt
```

You can also ask Claude to add an account (`workday_add_account` opens a native password dialog).

Only username/password accounts work. Accounts that sign in through Google or another SSO provider
can't be used.

## Registered sites index

Every account you add (wizard, CSV import, `workday_add_account`, `workday_save_credentials`) is recorded in a local
index of the employers you've registered, so the agent knows which companies it can look up:
company name, host, site, username, date added, and how it was added (`csv-import`, `dialog`,
`typed`, `wizard`). Run `work-mcp accounts` to see it, or ask Claude to call `workday_list_accounts`.
CSV imports use the entry's title (for example "Hooli") as the company name when it isn't just a
URL; otherwise the name is guessed from the URL and you can fix it with `workday_set_company`.

## Sites that log in with an emailed code (Oracle Recruiting Cloud, Greenhouse)

Some sites (Oracle Recruiting Cloud careers sites, MyGreenhouse) have no password: you enter your email and the site sends
a one-time code. `mcp-oracle` handles this by reading the code from an [AgentMail](https://agentmail.to)
inbox through its Python SDK:

1. Create an AgentMail inbox and set a forwarding rule in your own mailbox that sends the employer's
   login emails to it.
2. Run `uv run work-mcp mailbox`. It asks for the inbox address and API key, checks them, and stores the
   key in your keychain (or set `AGENTMAIL_API_KEY` and `AGENTMAIL_INBOX_ID`).
3. Add the employer with `uv run work-mcp add-oracle <url> <email>` (any `.../sites/<SITE>/...` URL from
   the site), `oracle_add_account`, or through `work-mcp setup`.

A refresh sends a code, waits for the forwarded email (up to two minutes), and logs in. Only emails that
arrive after the login started are considered, and each email is used once. How an employer's email is
recognised lives in `ats_core/mailbox.py` (built-in templates) or `<config>/email-templates.json`:
`{"acme": {"contains": "acme.com", "pattern": "code: (\\d{6})"}}`. The tools are `oracle_add_account`,
`oracle_list_accounts`, `oracle_set_company`, `oracle_delete_account`, `oracle_get_applications`,
`oracle_get_delta` and `oracle_get_status_history`, which work like their `workday_*` counterparts.

## Greenhouse (MyGreenhouse)

`mcp-greenhouse` covers your own account at my.greenhouse.io, which gathers applications to every employer
that uses Greenhouse. It signs in with your email and an 8-digit code, read from the same AgentMail inbox
(forward Greenhouse's emails there; the 8-digit pattern is built in). Add the account with
`uv run work-mcp add-greenhouse <email>` or `greenhouse_add_account`. The session cookie is cached and lasts
a long time, so the code is normally needed only once.

| Tool | What it does |
|---|---|
| `greenhouse_get_applications` | Your applications across employers (filters: `company`, `state`, `title`, `applied_after`; cached 30 minutes). |
| `greenhouse_get_delta` / `greenhouse_get_status_history` | What changed since the last call, and the statuses seen over time. |
| `greenhouse_mark_inactive` | Move an application to the inactive list in your own MyGreenhouse tracker. The employer isn't told; the site has no undo. |
| `greenhouse_get_recommendations` | The jobs MyGreenhouse recommends for you. |
| `greenhouse_search_jobs` | Search all Greenhouse jobs by keywords, a place name (`location`), date and page. |
| `greenhouse_add_account`, `greenhouse_list_accounts`, `greenhouse_delete_account`, `greenhouse_list_jobs` | Account setup, and a public job board listing with no login. |

Most employers share no progress, so an application is just "Active" or "Inactive". Where an employer does
share updates, its latest stage or activity (for example "Applied") is the status. Environment variables
use the `GREENHOUSE_` prefix (`GREENHOUSE_CACHE_DIR`, `GREENHOUSE_CACHE_TTL_MINUTES`).

## Work at a Startup (YC)

`mcp-waas` covers your own account at [workatastartup.com](https://www.workatastartup.com), Y Combinator's job
board. There is no application form there: applying means messaging a founder, so each of your applications is
a conversation with one company. The account is your YC login, which sends a 6-digit code to your email, read
from the same AgentMail inbox (forward YC's emails there; the generic 6-digit template is used). Add it with
`uv run work-mcp add-waas <email>` or `waas_add_account`.

| Tool | What it does |
|---|---|
| `waas_get_applications` | Your applications (conversations) with the company, job title, status and send date (filters: `company`, `state`, `title`, `applied_after`; cached 30 minutes). |
| `waas_get_delta` / `waas_get_status_history` | What changed since the last call (including a company replying), and the statuses seen over time. |
| `waas_get_conversation` | The full thread with one company. Doesn't mark it as read. |
| `waas_search_jobs` | Search jobs by text, `role` (`eng`), `job_type` (`intern`) and `remote` (`yes`). |
| `waas_get_job` | One job's details and whether you applied. |
| `waas_add_account`, `waas_list_accounts`, `waas_delete_account` | Account setup. |

The only status seen is "Reviewing"; other states, and how a rejection looks, are unverified. The login was tested live. Environment
variables use the `WAAS_` prefix.

## Tools

| Tool | What it does |
|---|---|
| `workday_list_accounts` | The index of registered employers: company, host, site, username, date added, source. Never returns passwords. |
| `workday_set_company` | Rename an employer's display name, e.g. Acme for the `acme` tenant. |
| `workday_save_credentials` | Save credentials passed as arguments. The password passes through the chat, so prefer `workday_add_account`. |
| `workday_add_account` | Add an employer from any of its careers-site URLs. Opens a native password dialog (macOS: native, Windows/Linux: Tk) whose input goes straight to your system keychain and never through the chat. |
| `workday_delete_credentials` | Remove an employer's credentials, cached session and snapshot. |
| `workday_get_applications` | Applications across **all** registered employers, or one. Filters: `company` (e.g. "hooli"), `account`, `state` (active/inactive/all), `status`, `title`, `applied_after`. Served from the local cache when it's under 30 minutes old; `refresh=true` forces a fresh login. Reports per-employer freshness and errors. |
| `workday_get_delta` | What changed since the last call, per employer: new applications, status changes, removed ones. Always refreshes first. The first call per employer is a baseline. `commit=false` peeks without updating the baseline. |
| `workday_get_status_history` | Every status we've seen for your applications, newest first (cache only, no login). `seen_at` is when we noticed the change, not when the employer made it. |

`company` matches the registered company name or host, case-insensitively. `account` is the exact host and wins over `company`. With neither, tools cover every registered employer.

## Where things live

| Path | Contents |
|---|---|
| System keychain, service `work-mcps` | Workday passwords and the AgentMail API key |
| `~/.config/work-mcps/` (`%APPDATA%\work-mcps` on Windows) | Non-secret account indexes (`workday-accounts.json`, `oracle-accounts.json`, `greenhouse-accounts.json`, `waas-accounts.json`), `mailbox.json` (AgentMail inbox id), optional `email-templates.json`, `keyring.json` (headless choice) |
| `~/.cache/work-mcps/<platform>/` for `workday`, `oracle`, `greenhouse`, `waas` (`%LOCALAPPDATA%\work-mcps\<platform>` on Windows) | `<platform>.db` (SQLite cache of all accounts' applications plus status history), cached login sessions `session-*.json` (mode 600), delta snapshots |

The cached sessions are as good as being signed in (Workday and Greenhouse cookies, Oracle's access code),
so keep the cache folders out of backups and sync folders. Deleting an account removes its session file.

Override with `<PLATFORM>_CONFIG_DIR` and `<PLATFORM>_CACHE_DIR` (`WORKDAY_`, `ORACLE_`, `GREENHOUSE_`, `WAAS_`); change
the cache lifetime with `<PLATFORM>_CACHE_TTL_MINUTES` (default 30). That lifetime applies to the application
data only: a saved login session is reused until the site rejects it, and then the server logs in again once.

## Progress

| Platform | Status |
|---|---|
| Workday | ✅ Done: list applications, track status changes |
| Oracle Recruiting Cloud | ✅ Done: email-code login, same tools as Workday |
| Greenhouse (MyGreenhouse) | ✅ Done: applications, recommendations, job search, mark inactive. Filters beyond keywords, place and date are not supported yet |
| Work at a Startup (YC) | ✅ Done: applications (founder conversations), job search. Rejection states not yet seen |
| Eightfold | ⏳ Pending |
| Dayforce | ⏳ Pending |
| Pinpoint (pinpointhq) | ❌ Not building |
| Darwinbox | ❌ Not building |
| TAL.net | ⏳ Pending |
| iCIMS | ⏳ Pending |
| amazon.jobs | ⏳ Pending |
| Google Careers | ⏳ Pending |
| IBM Careers | ❌ Not building |
| Other company ATS-es | 🔜 Coming soon |

## Contributing

PRs are welcome, especially new platforms. Each platform is its own package under `packages/`
that maps the site's candidate API into the shared models in `ats-core`; `CLAUDE.md` has the
architecture notes and a worked list of Workday quirks to learn from.

**Write tools will not be developed or merged, for safety reasons** (the one exception is MyGreenhouse's
own-tracker `greenhouse_mark_inactive`). That means anything that
applies, withdraws, edits a profile, uploads documents, or otherwise changes data on an employer's
site. Only read-only additions are accepted (see [Read-only by design](#read-only-by-design)).

Want a specific site supported? **Open an issue** (use the "Platform / site request" form) with the careers-site URL and what you'd like to
check (application status, saved jobs, and so on). Requests help decide what gets built next.

## Packages

| Package | Purpose |
|---|---|
| `mcp-workday` | Workday MCP server (done) |
| `mcp-oracle` | Oracle Recruiting Cloud MCP server (done): sites that log in with an emailed code |
| `mcp-greenhouse` | Greenhouse (MyGreenhouse) MCP server (done): applications, recommendations, job search |
| `mcp-waas` | Work at a Startup (YC) MCP server (done): applications, conversations, job search |
| `ats-core` | Shared models, credential store, snapshot diffing |
| `work-mcp` | `work-mcp` CLI: setup wizard, CSV credential import, server registration |

## Development

```sh
uv run pytest        # offline; uses an in-memory keyring, never your real keychain
uv run ruff check .
uv run pyright
```

To add a platform, create `packages/mcp-<name>` and depend on `ats-core`. See `CLAUDE.md` for
architecture notes and Workday quirks.

### Capturing traffic for a new platform

```sh
uv run playwright install chromium
uv run python tools/capture.py "<careers-site-login-url>"
```

Log in by hand, click around, close the window. Output goes to `.captures/` (gitignored) and
includes session tokens **and the password you typed**. Delete it when you're done.

## Read-only by design

The MCP tools only **read** from employer portals. They log in and fetch your application status;
they can't apply, withdraw, edit a profile, upload files, or change anything on an employer's side.
The single exception is `greenhouse_mark_inactive`, which only moves an application to the inactive list of
your own MyGreenhouse tracker (the employer is never told), so an agent can tidy it after a rejection.
That's deliberate: these servers hold your real logins and act on your behalf, so keeping them
read-only limits the damage a bug, a bad prompt, or a misbehaving agent can do. (The tools that
manage your local setup, such as adding or deleting a saved account, only touch files on your
machine.)

## Disclaimer

This is an unofficial, independent project. It isn't affiliated with, endorsed by, or sponsored by
Workday, Greenhouse, or any employer or ATS vendor named here, and all trademarks belong to their
owners.

It signs in to careers portals with your own credentials and reads your own data. Automated access
may be restricted by a site's terms of service, and employers may rate-limit or lock accounts after
repeated failed logins. You are responsible for checking the terms of the sites you connect and for
how you use this software. Passwords are stored in your operating system's keychain, and cached
application data stays on your machine, but you should still treat both as sensitive. The software
comes with no warranty (see the GPL-3.0 license). Use it at your own risk.

## License

Copyright (C) 2026 Krishaay.

Licensed under the GPL-3.0-or-later. See [LICENSE](./LICENSE).
