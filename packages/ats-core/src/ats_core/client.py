"""Thin async HTTP base class that platform clients extend."""

from typing import Any

import httpx


class ATSClient:
    def __init__(self, base_url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout)

    async def get(self, path: str, **params: Any) -> Any:
        resp = await self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def aclose(self) -> None:
        await self._http.aclose()
