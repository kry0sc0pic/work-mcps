# mcp-waas

MCP server for the candidate side of Work at a Startup (workatastartup.com, Y Combinator's job board): your own
applications and job search.

- `waas_add_account`, `waas_list_accounts`, `waas_delete_account`
- `waas_get_applications`, `waas_get_delta`, `waas_get_status_history`: each application is your conversation
  with one company (applying means messaging a founder)
- `waas_get_conversation`: the full thread, without marking it read
- `waas_search_jobs`, `waas_get_job`

Login goes through your YC account: an email address and a 6-digit code, read from an AgentMail inbox the
YC emails are forwarded to (`work-mcp mailbox`, then `work-mcp add-waas <email>`). The session cookies are
cached in `~/.cache/work-mcps/waas/`. Only the status "Reviewing" has been seen; rejections are unverified.
