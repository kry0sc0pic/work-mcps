# mcp-avature

MCP server for your own job applications on **Avature** candidate portals (`<employer>.avature.net`),
used by a number of large employers for careers sites. Read-only.

One account per employer portal, keyed by host (`acme.avature.net`). Login is the portal's email and password.

## Tools

| Tool | What it does |
| --- | --- |
| `avature_list_accounts` | The registered portals (never passwords). |
| `avature_set_company` | Set the display name of a portal. |
| `avature_delete_credentials` | Forget a portal's credentials, session and cache. |
| `avature_get_applications` | Applications across portals (title, status, application date), cached for 30 minutes. |
| `avature_get_delta` | What changed since the last call. |
| `avature_get_status_history` | Status changes seen so far. |

## Setup

```
uv run work-mcp add-avature https://acme.avature.net/en_US/careers --company Acme   # asks for the password
uv run playwright install chromium                                                 # once
```

Or import from a password-manager CSV with `work-mcp import` (rows for `*.avature.net` are picked up).

## Notes

- The portals sit behind an AWS WAF bot check that plain HTTP clients can't pass. When it appears, a headless
  Chromium (Playwright) loads the login page once and the `aws-waf-token` it earns is replayed by the normal
  HTTP client, with the same user agent. That is why Chromium must be installed. The token is kept in the
  session file, so the browser starts rarely.
- Applications come from the "Jobs I've applied to" table on the profile page; the id is the job id.
- Active/inactive is a keyword guess on the status text. Only "Qualifications Under Review" has been seen.
