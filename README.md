# work-mcps

MCP servers for the **candidate side** of applicant tracking systems (ATS): check where your job
applications stand without clicking through every employer's careers portal.

## Setup

Requires [uv](https://docs.astral.sh/uv/) on macOS, Windows or Linux (see [Platform support](#platform-support)).

```sh
git clone https://github.com/kry0sc0pic/job-application-mcps.git work-mcps && cd work-mcps
uv sync --all-packages
uv run work-mcp setup
```

The wizard walks through:
1. **Platforms:** which ATS platforms to connect (currently Workday).
2. **Logins:** import them from a password-manager CSV (Apple Passwords, Chrome, 1Password,
   Bitwarden), type them in one at a time, or skip.
3. **Registration:** adds each MCP server to Claude Code (`claude mcp add --scope user …`). If the
   `claude` CLI isn't installed, it prints the commands instead.

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
  bare WSL usually have none, so saving a login fails with a message saying so. There is deliberately no
  plaintext fallback.
- Where there is no graphical dialog, use `uv run mcp-workday-login <url> <email>` in a terminal.
- Config and cache files live in `~/.config/work-mcps` and `~/.cache/work-mcps` (XDG variables are
  honoured), or under `%APPDATA%` and `%LOCALAPPDATA%` on Windows.
- CI runs lint, type checks and tests on all three systems. The author has so far only tried it by
  hand on macOS, so please open an issue if something misbehaves on Windows or Linux.

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
uv run work-mcp register                 # (re)register the servers with Claude Code
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
| System keychain, service `work-mcps` | Passwords |
| `~/.config/work-mcps/` | Non-secret account index (`%APPDATA%\work-mcps` on Windows) |
| `~/.cache/work-mcps/workday/` (`%LOCALAPPDATA%\work-mcps\workday` on Windows) | `workday.db` (SQLite cache of all employers' applications plus status history), cached login sessions (mode 600), delta snapshots |

Override with `WORKDAY_CONFIG_DIR` and `WORKDAY_CACHE_DIR`; change the cache lifetime with `WORKDAY_CACHE_TTL_MINUTES` (default 30).

## Progress

| Platform | Status |
|---|---|
| Workday | ✅ Done: list applications, track status changes |
| Greenhouse | ⏳ Pending: `mcp-greenhouse` is a stub |
| Eightfold | ⏳ Pending |
| Dayforce | ⏳ Pending |
| Pinpoint (pinpointhq) | ⏳ Pending |
| Darwinbox | ⏳ Pending |
| TAL.net | ⏳ Pending |
| iCIMS | ⏳ Pending |
| amazon.jobs | ⏳ Pending |
| Google Careers | ⏳ Pending |
| IBM Careers | ⏳ Pending |
| Other company ATS-es | 🔜 Coming soon |

## Contributing

PRs are welcome, especially new platforms. Each platform is its own package under `packages/`
that maps the site's candidate API into the shared models in `ats-core`; `CLAUDE.md` has the
architecture notes and a worked list of Workday quirks to learn from.

**Write tools will not be developed or merged, for safety reasons.** That means anything that
applies, withdraws, edits a profile, uploads documents, or otherwise changes data on an employer's
site. Only read-only additions are accepted (see [Read-only by design](#read-only-by-design)).

Want a specific site supported? **Open an issue** (use the "Platform / site request" form) with the careers-site URL and what you'd like to
check (application status, saved jobs, and so on). Requests help decide what gets built next.

## Packages

| Package | Purpose |
|---|---|
| `mcp-workday` | Workday MCP server (done) |
| `mcp-greenhouse` | Greenhouse MCP server (stub: public job board listing only) |
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
