# work-mcps

MCP servers for the **candidate side** of applicant tracking systems (ATS): check where your job
applications stand without clicking through every employer's careers portal.

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

Want a specific site supported? **Open an issue** (use the "Platform / site request" form) with the careers-site URL and what you'd like to
check (application status, saved jobs, and so on). Requests help decide what gets built next.

## To do

- [ ] Password import for each new platform: add its `Platform` adapter so `work-mcp import`
      auto-populates its accounts from the same CSV (Workday is wired up).
- [ ] Import without a manual CSV step (direct from Apple Passwords / Chrome), if a safe route exists.
- [ ] Opt-in live smoke test for login and application fetch.
- [ ] Normalised application `stage` (submitted, in review, rejected, offer) across employers.

## Packages

| Package | Purpose |
|---|---|
| `mcp-workday` | Workday MCP server (done) |
| `mcp-greenhouse` | Greenhouse MCP server (stub: public job board listing only) |
| `ats-core` | Shared models, credential store, snapshot diffing |
| `work-mcp` | `work-mcp` CLI: setup wizard, CSV credential import, server registration |

## Setup

Requires [uv](https://docs.astral.sh/uv/) and macOS (credentials use the system Keychain via
[`keyring`](https://pypi.org/project/keyring/)).

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

Passwords go to the Keychain. Only the host, site name and username are written to
`~/.config/work-mcps/workday-accounts.json`. Run the wizard in a normal terminal, since the hidden
password prompt needs a TTY.

### Importing from a password manager

```sh
uv run work-mcp import ~/Downloads/passwords.csv --dry-run   # preview, saves nothing
uv run work-mcp import ~/Downloads/passwords.csv             # save, then offer to delete the CSV
```

Export the CSV from Apple Passwords (File → Export All Passwords…) or Chrome (Settings → Passwords →
Export). The file contains **every** password you own in plain text: only rows for supported
platforms are read, nothing is printed, and the tool offers to delete the file afterwards.
Rows are reported as `new`, `update`, `unchanged`, `conflict` (two different logins for one host) or
`skipped` (for example a URL with no site name, or a row with no saved password).

### Other commands

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
| `workday_add_account` | Add an employer from any of its careers-site URLs. Opens a native macOS dialog for the password, which goes straight to the Keychain and never through the chat. |
| `workday_delete_credentials` | Remove an employer's credentials, cached session and snapshot. |
| `workday_get_applications` | Applications across **all** registered employers, or one. Filters: `company` (e.g. "hooli"), `account`, `state` (active/inactive/all), `status`, `title`, `applied_after`. Served from the local cache when it's under 30 minutes old; `refresh=true` forces a fresh login. Reports per-employer freshness and errors. |
| `workday_get_delta` | What changed since the last call, per employer: new applications, status changes, removed ones. Always refreshes first. The first call per employer is a baseline. `commit=false` peeks without updating the baseline. |
| `workday_get_status_history` | Every status we've seen for your applications, newest first (cache only, no login). `seen_at` is when we noticed the change, not when the employer made it. |

`company` matches the registered company name or host, case-insensitively. `account` is the exact host and wins over `company`. With neither, tools cover every registered employer.

## Where things live

| Path | Contents |
|---|---|
| macOS Keychain, service `work-mcps` | Passwords |
| `~/.config/work-mcps/` | Non-secret account index |
| `~/.cache/work-mcps/workday/` | `workday.db` (SQLite cache of all employers' applications plus status history), cached login sessions (mode 600), delta snapshots |

Override with `WORKDAY_CONFIG_DIR` and `WORKDAY_CACHE_DIR`; change the cache lifetime with `WORKDAY_CACHE_TTL_MINUTES` (default 30).

## License

Copyright (C) 2026 Krishaay.

Licensed under the GPL-3.0-or-later. See [LICENSE](./LICENSE).

## Development

```sh
uv run pytest        # offline; uses an in-memory keyring, never your real Keychain
uv run ruff check .
uv run pyright
```

To add a platform, create `packages/mcp-<name>` and depend on `ats-core`. See `CLAUDE.md` for
architecture notes and Workday quirks.

### Capturing traffic for a new platform

```sh
uv run playwright install chromium
uv run python packages/mcp-workday/tools/capture.py "<careers-site-login-url>"
```

Log in by hand, click around, close the window. Output goes to `.captures/` (gitignored) and
includes session tokens **and the password you typed**. Delete it when you're done.
