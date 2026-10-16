import re
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel

_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.yello\.co$")


class YelloAccount(BaseModel):
    host: str

    @property
    def key(self) -> str:
        """Credentials are keyed by the portal host, e.g. acme.yello.co: one account per employer."""
        return self.host

    @property
    def tenant(self) -> str:
        return self.host.split(".")[0]

    @property
    def default_company(self) -> str:
        return self.tenant.replace("-", " ").replace("_", " ").title()

    def meta(self, source: str, company: str | None = None) -> dict[str, str]:
        """Non-secret fields stored in the account index."""
        return {"company": company or self.default_company, "source": source}

    @classmethod
    def from_url(cls, url: str) -> "YelloAccount":
        """A URL on an employer's `<tenant>.yello.co` portal. The sign-in page lives on a shared CAS host
        (`<region>-p-sso.yello.co`) whose `service` parameter names the portal, so that wins."""
        raw = url.strip().strip("<>\"'").replace("\\", "")
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        parts = urlparse(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        if "-sso." in host:
            service = parse_qs(parts.query).get("service", [""])[0]
            host = (urlparse(service).hostname or "").lower().rstrip(".")
        if not _HOST.match(host) or "-sso." in host:
            raise ValueError(f"Not a Yello candidate portal URL (expected <employer>.yello.co): {url!r}")
        return cls(host=host)

    @classmethod
    def from_key(cls, key: str) -> "YelloAccount":
        return cls(host=key)
