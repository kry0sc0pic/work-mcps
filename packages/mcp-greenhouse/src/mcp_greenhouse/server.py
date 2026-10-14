from ats_core import ATSClient, Job
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("greenhouse")

# Public Job Board API (no auth). Candidate-facing, so this is the natural starting point.
BOARD_API = "https://boards-api.greenhouse.io/v1/boards"


@mcp.tool()
async def greenhouse_list_jobs(board_token: str) -> list[Job]:
    """List open jobs on a company's Greenhouse job board."""
    client = ATSClient(BOARD_API)
    try:
        data = await client.get(f"/{board_token}/jobs")
    finally:
        await client.aclose()
    return [
        Job(
            id=str(j["id"]),
            title=j["title"],
            company=board_token,
            location=(j.get("location") or {}).get("name"),
            url=j.get("absolute_url"),
        )
        for j in data["jobs"]
    ]


def main() -> None:
    mcp.run()
