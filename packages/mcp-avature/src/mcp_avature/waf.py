"""Get past the AWS WAF challenge that fronts Avature portals.

Plain HTTP clients get a 202 "challenge" page. AWS's own script, run in a real browser, earns an
`aws-waf-token` cookie, and replaying that cookie (with the same user agent) lets ordinary requests through.
So a headless Chromium is started only to load the login page once; everything else is plain httpx.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx


class WafError(RuntimeError):
    pass


@dataclass
class WafPass:
    cookies: dict[str, str]
    user_agent: str


TokenProvider = Callable[[str], Awaitable[WafPass]]


def challenged(resp: httpx.Response) -> bool:
    return resp.headers.get("x-amzn-waf-action", "").lower() in ("challenge", "captcha") or (
        resp.status_code == 202 and "awswaf" in resp.text.lower()
    )


async def browser_pass(url: str) -> WafPass:
    """Load `url` in headless Chromium, wait for the WAF script to finish, and return its cookies."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as e:  # pragma: no cover
        raise WafError("Playwright is not installed; run `uv sync --all-packages`.") from e
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle")
                # The real page replaces the challenge page once the script has finished.
                await page.wait_for_function(
                    "!document.documentElement.innerHTML.includes('awswaf') || "
                    "document.querySelector('input[type=password]')",
                    timeout=30_000,
                )
                cookies = await page.context.cookies()
                agent = await page.evaluate("navigator.userAgent")
            finally:
                await browser.close()
    except PlaywrightError as e:
        if "Executable doesn't exist" in str(e):
            raise WafError(
                "This site needs a browser to get past its bot check. Run `uv run playwright install "
                "chromium` once, then try again."
            ) from e
        raise WafError(f"The browser could not pass the site's bot check: {e}") from e
    jar = {c.get("name", ""): c.get("value", "") for c in cookies}
    if "aws-waf-token" not in jar:
        raise WafError("The site's bot check did not issue a token.")
    return WafPass(jar, agent)
