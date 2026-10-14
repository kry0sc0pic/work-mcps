# work-mcps

MCP servers for the candidate side of ATS platforms (a job seeker's own applications, not
employer/recruiter tooling). Python 3.12, uv workspace.

## Commands
- `uv sync --all-packages`: a bare `uv sync` installs only the root and skips the server packages.
- Setup wizard: `uv run work-mcp setup` (also `import`, `accounts`, `register`). Needs a TTY.
- `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`: all must pass.
- Run a server: `uv run mcp-workday` (stdio).
- Save credentials: `uv run mcp-workday-login <url> <email>`. Needs a real TTY, so it fails under
  Claude Code's `!` shell. Ask the user to run it in their own terminal.

## Layout
- `packages/ats-core`: platform-agnostic `Application`/`Delta` models, `CredentialStore`
  (keyring + non-secret JSON index), `SnapshotStore` + `diff`. New platforms map into these models.
- `packages/mcp-workday`: `accounts.py` (URL parsing), `auth.py` (login), `client.py` (CXS API),
  `server.py` (tools), `prompt.py` (native dialog), `cli.py` (terminal password prompt), `tools/capture.py` (Playwright traffic recorder).
- `packages/work-mcp`: the `work-mcp` CLI (typer + rich). `platforms.py` is the registry
  (`Platform` protocol; add a platform here to make it appear in `setup` and `import`), `importer.py`
  parses password-manager CSVs into a dry-run `ImportPlan` before anything is saved, `cli.py` has the
  wizard and commands.
- `packages/mcp-greenhouse`: stub, not wired to the credential store.
- `tests/`: one flat directory. `conftest.py` swaps in an in-memory keyring so tests never touch the
  real Keychain.

## mcp SDK is 2.x
`from mcp.server.mcpserver import MCPServer`, not `FastMCP`. Most online examples are v1.
Ordinary exceptions in a tool are masked as a crash ("Error executing tool X", no detail); only
`ToolError` messages reach the model. The `@user_errors` decorator in `mcp_workday/server.py` converts
expected errors (ValueError, AuthError, cancelled dialog) so the agent can read and act on them. Put it
under `@mcp.tool()` on every tool.

## Workday quirks (verified against live Umbrella, Hooli and Initech tenants)
- Credentials and caches are keyed by **host** (`hooli.wd5.myworkdayjobs.com`); tenant = first host
  label; the site name is stored in the account index. One site per host.
- The login page only includes `sessionSecureToken` when the request sends
  `Accept: application/json, text/html`. A bare `*/*` gets a page without it.
- Login = GET `/en-US/{site}/login`, extract the token from the page JSON, form-POST. Success is a 303
  with a `CALYPSO_SESSION` cookie. A failed login is HTTP 200 JSON with a `message` field.
- `CALYPSO_CSRF_TOKEN` is never issued by plain HTTP login, and GETs work without it. It is sent only
  if present.
- `GET /wday/cxs/{tenant}/{site}/applications?type=active|inactive&offset&limit`: **`limit` above 8
  returns 400.** Page with `total`.
- Dates come back as both `28 August 2026` and `August 28, 2026`; `parse_date` handles both.
- Status text is free-form and differs per employer ("Not Selected", "Declined. Thank you for
  applying", "Application assessed: not selected"). Never enum-validate it.
- Session expiry shows as a redirect, 401/403, or a non-JSON body. The client re-logs in once and
  retries.
- SSO-only accounts are unsupported. `*.myworkdaysite.com` URLs are rejected.

## Application cache
`ats_core/cache.py`: one SQLite file per platform (`workday.db`) holding every account's applications
plus a `status_history` table (a row whenever a status is first seen or changes). Tools read it
instead of logging in each time (`WORKDAY_CACHE_TTL_MINUTES`, default 30). `sync_accounts` refreshes
accounts concurrently; one failing account reports an `error` and serves its cached rows rather than
failing the call. `get_delta` always forces a refresh and skips accounts whose refresh failed, so stale
data is never diffed against the baseline. Delta baselines stay in per-account JSON `SnapshotStore`s.
Tool filters (`company`, `status`, ...) run in Python over the loaded rows; fine at this scale.

## Registered-sites index
`CredentialStore`'s JSON index is the agent-visible list of known employers. Per account it holds
non-secret fields only: `username`, `added_at` (kept when a login is re-saved), plus platform meta
(Workday: `site`, `company`, `source`). Platforms put these in `AccountRef.meta`. The CSV importer feeds
the entry title in as the company hint unless it looks like a URL.

## CSV import UX
`work-mcp import` parses first, then shows one `questionary` checkbox row per platform
(`Workday: N passwords`, counted by host match alone). Rows whose URL lacks details (Workday
host-only URLs) get status `lookup` and are resolved by `Platform.discover` only after the user
confirms (Workday: the `Sitemap:` lines in the host's public robots.txt). Don't make the count
depend on that lookup. Piped stdin (tests, scripts) skips the TUI and asks a y/n confirm instead.

## Read-only policy
Tools that change data on an employer's site (apply, withdraw, edit profile, upload) are out of scope
and will not be merged, for safety: the servers hold real logins. Platform tools may log in and issue
GET requests only. Local-setup tools (add/delete/rename an account) only touch the user's own files and
keychain. If a request would need a write call to an employer, say no and point to this policy.

## Security rules
- Never print, log or commit passwords or session cookies. `.captures/` holds plaintext passwords
  from the login POST and is gitignored; treat it as secret.
- `workday_save_credentials` takes a password argument, so it passes through the chat. Prefer `workday_add_account`,
  which collects the password in a native `osascript` dialog (`prompt.py`; macOS only) and writes it
  straight to the keychain. Dialog text goes in as argv, never interpolated into AppleScript.
- Live tests against a real account are read-only GETs plus login. Use a throwaway
  `WORKDAY_CACHE_DIR` so the user's real delta baseline isn't consumed. Don't trigger writes
  (withdraw, apply) without the user's explicit say-so.

- CSV exports contain every password the user owns. The importer reads only matching rows, never
  prints passwords, and offers to delete the file. Keep it that way.
- `work-mcp register` runs `claude mcp add --scope user …`, which edits the user's Claude config.
  Don't run it for real without asking; tests stub `shutil.which`.

## License
GPL-3.0-or-later (`LICENSE`).

## Conventions
- MCP tool names are prefixed with the platform id (`workday_get_applications`, `greenhouse_list_jobs`) so
  several platform servers can be registered side by side without clashes. Do this for every new platform.
- Line length 110; ruff rules E, F, I, UP, B.
- Comments only for non-obvious "why", especially API quirks like the above.
