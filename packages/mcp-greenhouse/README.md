# mcp-greenhouse

MCP server for the candidate side of Greenhouse (my.greenhouse.io): your own applications, recommendations
and job search.

- `greenhouse_add_account`, `greenhouse_list_accounts`, `greenhouse_delete_account`
- `greenhouse_get_applications`, `greenhouse_get_delta`, `greenhouse_get_status_history`
- `greenhouse_mark_inactive`: move an application to the inactive list in your own MyGreenhouse tracker
  (the employer isn't told)
- `greenhouse_get_recommendations`, `greenhouse_search_jobs`
- `greenhouse_list_jobs`: open jobs on a company's public job board (no login)

MyGreenhouse logs in with an email and an 8-digit code, which is read from an AgentMail inbox the
Greenhouse emails are forwarded to (`work-mcp mailbox`). The session lasts a long time, so the code is
normally needed once. Most employers share nothing but active/inactive; for those that do, the latest
stage or activity is shown as the status.
