import re
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

# en, en-US, fr_CA, zh-Hans-CN ... only treated as a locale if more path follows it.
_LOCALE = re.compile(r"^[a-z]{2,3}([-_][A-Za-z]{2,4}){0,2}$")
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.myworkdayjobs\.com$")


class WorkdayAccount(BaseModel):
    host: str
    tenant: str
    site: str

    @property
    def key(self) -> str:
        """Credentials are keyed by host, e.g. umbrella.wd5.myworkdayjobs.com."""
        return self.host

    @property
    def default_company(self) -> str:
        return self.tenant.replace("-", " ").replace("_", " ").title()

    def meta(self, source: str, company: str | None = None) -> dict[str, str]:
        """Non-secret fields stored in the account index."""
        return {"site": self.site, "company": company or self.default_company, "source": source}

    @classmethod
    def from_url(cls, url: str) -> "WorkdayAccount":
        """Parse any Workday careers URL: login, job, apply, or API links, with or without a
        scheme, locale, trailing slash, query string or fragment."""
        raw = url.strip().strip("<>\"'").replace("\\", "")  # pasted shell escapes like \? \=
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        u = urlparse(raw)
        host = (u.hostname or "").lower().rstrip(".")
        if not _HOST.match(host):
            raise ValueError(f"Not a Workday careers URL (expected *.myworkdayjobs.com): {url!r}")
        parts = [unquote(p) for p in u.path.split("/") if p]
        if len(parts) >= 4 and parts[:2] == ["wday", "cxs"]:  # API URL: /wday/cxs/{tenant}/{site}/...
            return cls(host=host, tenant=parts[2], site=parts[3])
        if len(parts) > 1 and _LOCALE.match(parts[0]):
            parts = parts[1:]
        elif len(parts) == 1 and re.match(r"^[a-z]{2,3}[-_][A-Za-z]{2,4}$", parts[0]):
            parts = []  # a bare locale like /en-US/ is not a site
        if not parts:
            raise ValueError(f"No site name in URL (expected /<locale>/<site>/...): {url!r}")
        return cls(host=host, tenant=host.split(".")[0], site=parts[0])

    @classmethod
    def from_key(cls, key: str, site: str) -> "WorkdayAccount":
        return cls(host=key, tenant=key.split(".")[0], site=site)
