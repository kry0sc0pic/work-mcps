import re
from urllib.parse import urlparse

from pydantic import BaseModel

_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.avature\.net$")
# `/en_US/careers`: locale + portal name. Portals can be named differently, so it is kept per account.
_PORTAL = re.compile(r"^/([a-z]{2}_[A-Z]{2})/([A-Za-z0-9_-]+)")
DEFAULT_PORTAL = "/en_US/careers"


class AvatureAccount(BaseModel):
    host: str
    portal: str = DEFAULT_PORTAL

    @property
    def key(self) -> str:
        """Credentials are keyed by host, e.g. acme.avature.net: one candidate account per employer portal."""
        return self.host

    @property
    def tenant(self) -> str:
        parts = self.host.split(".")
        # Custom domains (apply.example.com) have no tenant label of their own; use the company's domain name.
        return parts[0] if self.host.endswith(".avature.net") or len(parts) < 2 else parts[-2]

    @property
    def default_company(self) -> str:
        return self.tenant.replace("-", " ").replace("_", " ").title()

    def meta(self, source: str, company: str | None = None) -> dict[str, str]:
        """Non-secret fields stored in the account index."""
        return {"company": company or self.default_company, "source": source, "portal": self.portal}

    @classmethod
    def from_url(cls, url: str) -> "AvatureAccount":
        """A careers portal URL, with or without a scheme: `<tenant>.avature.net`, or a company's own domain
        (apply.example.com) as long as the path looks like an Avature `/en_US/careers` portal."""
        raw = url.strip().strip("<>\"'").replace("\\", "")
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        parts = urlparse(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        m = _PORTAL.match(parts.path)
        if not host or not (_HOST.match(host) or m):
            raise ValueError(f"Not an Avature portal URL (<employer>.avature.net or /en_US/careers): {url!r}")
        return cls(host=host, portal=f"/{m.group(1)}/{m.group(2)}" if m else DEFAULT_PORTAL)

    @classmethod
    def from_key(cls, key: str, portal: str | None = None) -> "AvatureAccount":
        return cls(host=key, portal=portal or DEFAULT_PORTAL)
