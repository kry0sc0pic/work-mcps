import re
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

_SITE = re.compile(r"/sites/([A-Za-z0-9_-]+)")
_API_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.fa\.[a-z0-9-]+\.oraclecloud\.com", re.I)
_GENERIC_LABELS = {"www", "careers", "career", "jobs", "job", "recruiting", "apply", "talent"}


class OracleAccount(BaseModel):
    host: str  # the careers site's host; also the credential/cache key (one site per host)
    site: str  # Oracle "site number", e.g. CX_1
    api_host: str  # where /hcmRestApi lives; equals `host` on Oracle-hosted sites

    @property
    def key(self) -> str:
        return self.host

    @property
    def default_company(self) -> str:
        labels = self.host.split(".")
        if self.host.endswith("oraclecloud.com"):
            return self.site  # the pod name says nothing about the employer
        name = next((x for x in labels[:-1] if x not in _GENERIC_LABELS), labels[0])
        return name.replace("-", " ").title()

    @property
    def default_mail_template(self) -> str:
        return re.sub(r"[^a-z0-9]", "", self.default_company.lower())

    def meta(
        self, source: str, company: str | None = None, mail_template: str | None = None
    ) -> dict[str, str]:
        """Non-secret fields stored in the account index."""
        return {
            "site": self.site,
            "api_host": self.api_host,
            "company": company or self.default_company,
            "mail_template": mail_template or self.default_mail_template,
            "auth": "email-code",
            "source": source,
        }

    @classmethod
    def host_and_site(cls, url: str) -> tuple[str, str]:
        raw = url.strip().strip("<>\"'")
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        u = urlparse(raw)
        host = (u.hostname or "").lower().rstrip(".")
        site = _SITE.search(u.path)
        if not host or not site:
            raise ValueError(
                f"Not an Oracle careers URL (expected .../sites/<SITE>/...): {url!r}. "
                "Open the site's sign-in page and copy that URL."
            )
        return host, site.group(1)

    @classmethod
    def from_url(cls, url: str, api_host: str | None = None) -> "OracleAccount":
        """Parse a careers URL. On Oracle-hosted sites the host is the API host; on custom domains the
        API host must be given or found with `discover_api_host`."""
        host, site = cls.host_and_site(url)
        if api_host is None and host.endswith(".oraclecloud.com"):
            api_host = host
        if api_host is None:
            raise ValueError(f"Can't tell which Oracle server {host} uses; use discover_api_host first.")
        return cls(host=host, site=site, api_host=api_host)

    @classmethod
    def from_key(cls, key: str, meta: dict[str, str]) -> "OracleAccount":
        return cls(host=key, site=meta["site"], api_host=meta["api_host"])


def discover_api_host(url: str) -> OracleAccount:
    """Custom-domain careers pages embed their Oracle server's host; read it from the public page."""
    host, site = OracleAccount.host_and_site(url)
    if host.endswith(".oraclecloud.com"):
        return OracleAccount(host=host, site=site, api_host=host)
    try:
        resp = httpx.get(
            f"https://{host}/en/sites/{site}/my-profile/sign-in",
            headers={"user-agent": "Mozilla/5.0"},
            follow_redirects=True,
            timeout=20,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ValueError(f"couldn't reach {host} to find its Oracle server ({e.__class__.__name__})") from e
    hosts = {h.lower() for h in _API_HOST.findall(resp.text)}
    if len(hosts) != 1:
        raise ValueError(
            f"{host} doesn't look like an Oracle Recruiting Cloud site (found {len(hosts)} hosts)."
        )
    return OracleAccount(host=host, site=site, api_host=hosts.pop())
