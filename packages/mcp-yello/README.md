# mcp-yello

MCP server for your own job applications on **Yello** candidate portals (`<employer>.yello.co`). Read-only.

One account per employer portal, keyed by host (`acme.yello.co`). Login is the portal's email and password.

## Tools

| Tool | What it does |
| --- | --- |
| `yello_list_accounts` | The registered portals (never passwords). |
| `yello_set_company` | Set the display name of a portal. |
| `yello_delete_credentials` | Forget a portal's credentials, session and cache. |
| `yello_get_applications` | Applications across portals, cached for 30 minutes. |
| `yello_get_delta` | What changed since the last call. |
| `yello_get_status_history` | Status changes seen so far. |
| `yello_get_schedule` | Invitations, events and interviews (live). |

## Setup

```
uv run work-mcp add-yello https://acme.yello.co/login --company Acme   # asks for the password
```

## Notes

The portal shows no application date. Only "Application Submitted" has been seen as a status.
