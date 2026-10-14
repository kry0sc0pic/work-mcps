"""Open a headed browser on any careers site and record all network traffic, to reverse-engineer its
candidate API when building support for a new platform.

You log in by hand (credentials never pass through this script). Everything is written to
.captures/<host>-<timestamp>/ (gitignored): a HAR with response bodies, a requests.jsonl of XHR/fetch
calls, and storage_state.json (cookies/localStorage) for replaying the session later.
Treat that directory as secret: it contains your session tokens and the password you typed
(it appears in the login POST body).

    uv run python tools/capture.py <careers-site-login-url>
"""

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Request, Response, async_playwright

ROOT = Path(__file__).resolve().parents[1]


async def main(url: str) -> None:
    host = urlparse(url).hostname or "site"
    out = ROOT / ".captures" / f"{host}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)
    log = (out / "requests.jsonl").open("a")

    async def on_request(req: Request) -> None:
        if req.resource_type not in ("xhr", "fetch", "document"):
            return
        rec = {
            "t": "req",
            "method": req.method,
            "url": req.url,
            "type": req.resource_type,
            "headers": await req.all_headers(),
            "body": req.post_data,
        }
        log.write(json.dumps(rec) + "\n")
        log.flush()

    async def on_response(resp: Response) -> None:
        if resp.request.resource_type not in ("xhr", "fetch", "document"):
            return
        try:
            body = (await resp.text())[:200_000]
        except Exception:
            body = None
        log.write(json.dumps({"t": "resp", "status": resp.status, "url": resp.url, "body": body}) + "\n")
        log.flush()

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            ROOT / ".browser-profile" / host,
            headless=False,
            record_har_path=out / "capture.har",
            record_har_content="embed",
            viewport=None,
        )
        ctx.on("request", lambda r: asyncio.ensure_future(on_request(r)))
        ctx.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url)
        print(f"Capturing to {out}\nLog in in the browser window, click around, then close the window.")
        closed = asyncio.Event()
        ctx.on("close", lambda _: closed.set())
        while not closed.is_set():
            try:
                await ctx.storage_state(path=out / "storage_state.json")
            except Exception:
                break
            try:
                await asyncio.wait_for(closed.wait(), timeout=5)
            except TimeoutError:
                pass
    log.close()
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
