import re
from urllib.parse import urlparse

from pydantic import BaseModel

_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.tal\.net$")


class TalAccount(BaseModel):
    host: str

    @property
    def key(self) -> str:
        """Credentials are keyed by host, e.g. acme.tal.net: one candidate account per employer portal."""
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
    def from_url(cls, url: str) -> "TalAccount":
        """Any URL on a `<tenant>.tal.net` portal: login, job board or candidate pages, with or without a
        scheme. Only the host matters."""
        raw = url.strip().strip("<>\"'").replace("\\", "")
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        host = (urlparse(raw).hostname or "").lower().rstrip(".")
        if not _HOST.match(host):
            raise ValueError(f"Not a TAL.net portal URL (expected <employer>.tal.net): {url!r}")
        return cls(host=host)

    @classmethod
    def from_key(cls, key: str) -> "TalAccount":
        return cls(host=key)
