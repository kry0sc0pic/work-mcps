# mcp-tal

MCP server for your own job applications on **TAL.net** (Oleeo) candidate portals, which many
employers use for graduate and campus recruiting (`<employer>.tal.net`). Read-only.

One account per employer portal, keyed by host (`acme.tal.net`). Login is the portal's email and password.

## Tools

| Tool | What it does |
| --- | --- |
| `tal_list_accounts` | The registered portals (never passwords). |
| `tal_set_company` | Set the display name of a portal. |
| `tal_delete_credentials` | Forget a portal's credentials, session and cache. |
| `tal_get_applications` | Applications across portals, cached for 30 minutes. |
| `tal_get_delta` | What changed since the last call. |
| `tal_get_status_history` | Status changes seen so far. |
| `tal_get_application` | One application: status message, emails received, program description. |
| `tal_get_message` | One email the employer sent. |

## Setup

```
uv run work-mcp add-tal https://acme.tal.net/candidate --company Acme   # asks for the password
```

Or import from a password-manager CSV with `work-mcp import` (rows for `*.tal.net` are picked up).

## Notes

- TAL.net shows no application date, so `applied_on` is empty. The portal's list only says
  "Application Update", so the status also includes the employer's message from the application page.
- Some visits get a "Quick Check Needed" page (a proof of work); the server solves it itself.
- Active/inactive is a keyword guess on the status text. Rejection wording has not been seen yet.
