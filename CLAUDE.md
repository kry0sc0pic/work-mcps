# work-mcps

MCP servers for the candidate side of ATS platforms (a job seeker's own applications, not
employer/recruiter tooling). Python 3.12, uv workspace.

## Commands
- `uv sync --all-packages`: a bare `uv sync` installs only the root and skips the server packages.
- Setup wizard: `uv run work-mcp setup` (also `import`, `accounts`, `register [-c client]`). Needs a TTY.
- `uv run pytest`, `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`: all must pass.
- Run a server: `uv run mcp-workday` (stdio).
- Save credentials: `uv run mcp-workday-login <url> <email>`. Needs a real TTY, so it fails under
  Claude Code's `!` shell. Ask the user to run it in their own terminal.

## Layout
- `packages/ats-core`: platform-agnostic `Application`/`Delta` models, `CredentialStore`
  (keyring + non-secret JSON index), `SnapshotStore` + `diff`. New platforms map into these models.
- `packages/mcp-workday`: `accounts.py` (URL parsing), `auth.py` (login), `client.py` (CXS API),
  `server.py` (tools), `prompt.py` (native dialog), `cli.py` (terminal password prompt).
- `packages/work-mcp`: the `work-mcp` CLI (typer + rich). `platforms.py` is the registry
  (`Platform` protocol; add a platform here to make it appear in `setup` and `import`), `importer.py`
  parses password-manager CSVs into a dry-run `ImportPlan` before anything is saved, `cli.py` has the
  wizard and commands.
  `clients.py` registers servers with AI clients: CLIs (`claude`/`codex`/`grok` `mcp add NAME -- CMD`,
  `openclaw mcp set NAME JSON`), a JSON edit (Cursor `mcpServers`) and a ruamel YAML edit (Hermes
  `mcp_servers`). Never overwrite an existing entry; write files atomically.
- `packages/mcp-oracle`: Oracle Recruiting Cloud (HCM candidate experience).
  Same shape as mcp-workday (`oracle_*` tools, own `oracle.db` cache) but email-code login: `auth.py`
  requests a code, `ats_core.mailbox.wait_for_code` reads it from AgentMail, the access code goes in
  headers. `accounts.py` finds the Oracle API host from the careers page.
- `packages/mcp-greenhouse`: MyGreenhouse (my.greenhouse.io) candidate side, `greenhouse_*` tools, own
  `greenhouse.db`. One account keyed `my.greenhouse.io` (username = login email); email-code login like Oracle.
  Also keeps the old public-board `greenhouse_list_jobs`.
- `packages/mcp-waas`: Work at a Startup (YC), `waas_*` tools, own `waas.db`. Email-code login via YC's
  account service; applications are founder conversations.
- `packages/mcp-tal`: TAL.net (Oleeo) candidate portals, `tal_*` tools, own `tal.db`. Email + password per
  `<employer>.tal.net` host (credential key = host); server-rendered HTML, scraped in `parse.py`.
- `packages/mcp-avature`: Avature candidate portals (`<employer>.avature.net`), `avature_*` tools, own `avature.db`.
  Email + password per host; the portal path (`/en_US/careers`) is kept in the account meta. `waf.py` gets the
  AWS WAF token with headless Playwright; everything else is httpx.
- `tools/capture.py`: platform-agnostic Playwright traffic recorder for reverse-engineering a new
  site (`uv run python tools/capture.py <login-url>`). Output goes to gitignored `.captures/`.
- `tests/`: one flat directory. `conftest.py` swaps in an in-memory keyring so tests never touch the
  real Keychain.

## mcp SDK is 2.x
`from mcp.server.mcpserver import MCPServer`, not `FastMCP`. Most online examples are v1.
Ordinary exceptions in a tool are masked as a crash ("Error executing tool X", no detail); only
`ToolError` messages reach the model. The `@user_errors` decorator in `mcp_workday/server.py` converts
expected errors (ValueError, AuthError, cancelled dialog) so the agent can read and act on them. Put it
under `@mcp.tool()` on every tool.

## Workday quirks (verified against live tenants)
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

## Oracle Recruiting Cloud quirks (verified against a live tenant)
- API host is `<pod>.fa.<region>.oraclecloud.com` (`/hcmRestApi/resources/latest/...`), found in the
  careers page HTML on custom domains. Site number comes from the `/sites/<SITE>/` path (CX_1).
- Login: POST `recruitingCEVerificationTokens` `{SiteNumber, Mode:"CSS_ACCESS", CaptchaToken:"", Control:"",
  Email}` with header `ora-irc-rest-action: CSS_ACCESS:EMAIL` -> `ChallengeFlag:true` and a code is
  emailed; POST again `{ShortCode, ChallengeFlag:true, Email}` -> `AccessCode`. Later requests send
  `ora-irc-access-code` + `ora-irc-cx-userid` (a client-made UUID). No cookies; Akamai on the careers
  domain does not guard the API host, so plain httpx works.
- `recruitingCEJobApplications` (limit 499, `hasMore`) has the status text, `ActiveFlag`, `SubmissionId`.
  `recruitingCEJobApplicationDrafts` answers 403 unless `ora-irc-rest-action: CE_JA_DRAFT` is sent.
- A login is an email to the user: don't loop or test live without being asked. Each email's code is
  consumed once (`mailbox._used`), and only mail newer than the login start counts.

## MyGreenhouse quirks (from a captured browser session; the login code path is not yet verified live)
- Rails + Inertia.js. Login: GET `/users/sign_in` (cookie `MYGREENHOUSE-XSRF-TOKEN`, page version in the
  `data-page` JSON), POST `/users/sign_in` `{email, job_board:null, device_type}` emails an **8-digit** code,
  POST `/users/submit_code` `{code, email, time_zone, device_type}`. POSTs send `x-inertia: true`,
  `x-inertia-version`, `x-requested-with: XMLHttpRequest` and `x-csrf-token` = the XSRF cookie value.
  Success is assumed when the reply's `props.currentUserId` is set. The session cookie is `_session_id`.
- `GET /applications.json?page=N&active_only=true` (and `inactive_only=true`): `{active|inactive:
  {applications, total_pages, count}}`. Each application carries its own `company_name`, which is why the
  cache has a per-row `company` column.
- Status depends on the employer. `sharingStatus: false` (most): only `inactive` true/false, so status is
  "Active"/"Inactive". `sharingStatus: true`: the detail view (`GET /applications/<id>` with `x-inertia`
  and partial headers `selected_application_id,selected_application`) has `activityHistory`
  (`[{date, type:"applied"}]`) and `stages`; we show the latest. `stages` was empty and only `applied` was
  ever seen, so how a rejection or stage is written is unverified (`client.status_text` guesses `name`/`type`).
- `PATCH /applications/<id>/mark_inactive` (no body, `x-csrf-token` = XSRF cookie, accept json) answers
  `{"success":true}` and moves it to the inactive list. See the read-only policy exception below.
- `GET /recommendations` (accept json) is a plain list; `GET /jobs/search?...` is Inertia and the deferred
  `jobPosts` come from a partial reload (`x-inertia-partial-component: jobs`, `-data:
  page,moreResultsAvailable,jobPosts`). Search params seen: query, location, lat, lon, location_type,
  country_short_name, state_short_name, date_posted=past_five_days, page.

## Work at a Startup quirks (verified live, including the login)
- Package `mcp-waas`, tools `waas_*`, one account keyed `workatastartup.com`. Applying is a message to a
  founder, so `GET /api/conversations` (JSON list of stubs, `state` "reviewing", `has_applied`,
  `referenced_job_ids`, `messages`) is the application list; `GET /api/conversations/<slug>` is one thread.
  Opening a thread makes the site POST `is_read`; we never do. Job titles come from `GET /jobs/<id>`
  (`data-page` props: `job`, `company`, `appliedJobIds`, `savedJobIds`). No CSRF header is needed on GETs.
- Login is YC's account service: GET `account.ycombinator.com/?continue=<site>` (CSRF in the `csrf-token`
  meta), then JSON POSTs with `x-csrf-token`: `/start_sign_in`, `/login_link` (emails a 6-digit code),
  `/verify_otp` `{otp_code}`; a redirect chain (`authenticate_token`, `sso-auth.workatastartup.com/set_auth`)
  ends with cookies `_bf_session_key`, `_sso.key`, `XSRF-TOKEN` on www. `verify_otp`'s reply is unread;
  `auth._token_url` handles a JSON token if one ever comes, and the redirects did the rest. Success is
  `navBar.loggedIn` on `/companies`. www answers **406 to a bare `*/*` Accept**, so a browser-like `accept`
  is a default header. httpx's INFO log carries the login token in URLs, so the server mutes it.
- Search: the `/companies` page embeds `window.AlgoliaOpts = {app, key}` (a secured key, per session). POST
  Algolia `queries` on index `WaaSPublicCompanyJob_created_at_desc_production` with `filters`
  `(role:eng) AND (remote:yes) AND (job_type:"intern")`, `distinct=true`; hits carry `objectID` (job) and
  `company_id`. `POST /companies/fetch {ids}` (with `x-csrf-token`) returns the companies, but their `jobs`
  lists omit some jobs the search found, so those come from the job page.

## TAL.net quirks (verified live, including the scripted login)
- Legacy server-rendered HTML, no JSON API. `/candidate/application` (table: title, status label, link ending
  `/application/<id>`), `/candidate/application/<id>` (`app-status` heading, `app-status-desc` message),
  `.../correspondence` (table `history-of-comms`: dd/mm/yyyy, subject, `.../email/<id>`), `.../email/<id>`,
  `.../opportunity` (eform, read as plain text). After a scripted login only URLs under the
  `/vx/lang-en-GB/mobile-0/brand-2/user-<n>/xf-<token>` prefix (where the login redirect lands) are signed in;
  bare `/candidate/...` ones show the signed-out page. So `auth.Session` keeps that prefix next to the cookies.
- The list label is "Application Update" for everything, so status = label + the employer's first message
  paragraph. No application date exists. Rejection wording is unseen (`_CLOSED` is a guess).
- Login: GET `/candidate/login`, form `login_form` (hidden `formtt_uuid`, `__vxXSRF_Token`, `...`; its `action`
  carries an `xf-` token), POST fields + `user`, `password`, `form_submit=Login`; success lands on a page with
  `class="logout_button"` (the signed-out page only has a hidden `logout_form`). Cookies that matter:
  `wcn_session` and `bypassCaptcha`. An expired session is a 200 signed-out page, so check the logout link.
- Some visits get "Quick Check Needed" (altcha proof of work): GET `?oleeoProtectCaptcha=1` ->
  `{algorithm: SHA-512, challenge, salt, maxnumber, signature}`; find n with sha512(salt+n) == challenge, POST
  `altcha=base64(json{algorithm,challenge,number,salt,signature,took})` and `originalRequestToken` back to the
  same URL. Cloudflare doesn't block plain httpx here. The login form has a reCAPTCHA slot that stayed empty.

## Avature quirks (login not yet verified live; the WAF step and page shapes come from a capture)
- Fronted by AWS WAF: a bare request gets HTTP 202 with `x-amzn-waf-action: challenge`. A headless Chromium
  that loads `<portal>/Login` earns an `aws-waf-token` cookie; replaying just that cookie, with the browser's
  user agent, makes plain httpx get the real page. `client._solve_waf` does this on any challenge and
  `_login` keeps that cookie when it clears the old session. Needs `playwright install chromium`.
- Login: GET `<portal>/Login`, POST `username`, `password`, `login` to the same URL (no hidden fields); success
  is a 302 to `<portal>/Profile`. A failed login re-renders the form. Signed in = a `.../Logout/` link.
- Applications are the "Jobs I've applied to" table on `<portal>/Profile`: title (link `JobDetail?jobId=N`),
  location, status, application date `MM/DD/YYYY`. Whether it paginates is unknown (4 rows seen). No detail
  pages have been captured. Rejection wording is unseen (`_CLOSED` is a guess).

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

## Cross-platform
Must work on macOS, Windows and Linux (CI runs all three; only macOS has been hand-tested).
- Keychain access goes through `CredentialStore`, which turns `keyring` errors into `CredentialStoreError`
  (e.g. no Secret Service on headless Linux). No *automatic* fallback to a file: the OS keychain is the default. Headless Linux can opt in
  explicitly with `work-mcp headless` (an Argon2/AES encrypted file, or, only if the user asks for it,
  a mode-600 plain-text file); the choice is saved in `keyring.json` (`ats_core/filekeyring.py`). Never
  select either without that explicit choice, and never on macOS/Windows.
- Native dialog (`prompt.py`): `osascript` on macOS, a Tk subprocess elsewhere; both take values as argv.
- Paths: XDG on macOS/Linux, `%APPDATA%` / `%LOCALAPPDATA%` on Windows. Pass `encoding="utf-8"` to every
  text file read/write (Windows defaults to cp1252) and don't assume `/` separators or `chmod` effects.
- Tests that fake the OS monkeypatch `sys.platform` (it is global, so keep those tests short).

## Read-only policy
Tools that change data on an employer's site (apply, withdraw, edit profile, upload) are out of scope
and will not be merged, for safety: the servers hold real logins. Platform tools may log in and issue
GET requests only. Local-setup tools (add/delete/rename an account) only touch the user's own files and
keychain. If a request would need a write call to an employer, say no and point to this policy.
One exception: `greenhouse_mark_inactive` only moves an application to the inactive list of the user's own
MyGreenhouse tracker. The employer is never told, so it is allowed. It exists so an agent that reads a
rejection email can tidy the tracker.

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
- `work-mcp register` edits the user's AI-client configs (Claude, Codex, Grok, OpenClaw via their CLIs;
  Cursor and Hermes files directly). Don't run it for real without asking. Tests use a fake `HOME`
  (conftest `fake_home`) and stub `shutil.which`.

## License
GPL-3.0-or-later (`LICENSE`).

## Conventions
- MCP tool names are prefixed with the platform id (`workday_get_applications`, `greenhouse_list_jobs`) so
  several platform servers can be registered side by side without clashes. Do this for every new platform.
- Line length 110; ruff rules E, F, I, UP, B.
- Comments only for non-obvious "why", especially API quirks like the above.
