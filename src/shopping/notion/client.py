"""Minimal Notion REST client (data-source API, version 2025-09-03).

Paced to Notion's ~3 requests/second, with retries on 429/5xx. Only the endpoints the app needs exist here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
_ID = re.compile(r"^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class NotionError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def clean_id(value: str) -> str:
    """Accept a raw id, collection:// URL or notion.so URL; return a dashed UUID."""
    v = (value or "").strip().replace("collection://", "").split("?")[0].rstrip("/")
    v = v.rsplit("/", 1)[-1].rsplit("-", 1)[-1] if ("/" in v or (len(v) > 36 and "-" in v)) else v
    raw = v.replace("-", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise NotionError(f"Not a Notion id: {value!r}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}".lower()


class NotionClient:
    MIN_INTERVAL = 0.35

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        if not token:
            raise NotionError("No Notion token set. Add the integration token in Settings → Notion.")
        self._http = httpx.AsyncClient(base_url=API, timeout=httpx.Timeout(30, connect=10), transport=transport,
                                       headers={"authorization": f"Bearer {token}", "notion-version": VERSION,
                                                "content-type": "application/json"})
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
        for attempt in range(4):
            async with self._lock:
                wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self._http.request(method, path, json=json, params=params)
                except httpx.TransportError as exc:
                    self._last = time.monotonic()
                    if attempt == 3:
                        raise NotionError(f"Notion unreachable ({type(exc).__name__}).") from exc
                    await asyncio.sleep(1 + attempt)
                    continue
                self._last = time.monotonic()
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == 3:
                    break
                await asyncio.sleep(float(r.headers.get("retry-after") or (1 + attempt * 2)))
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("message") or r.text
                except ValueError:
                    msg = r.text
                hint = " (is the database shared with the integration?)" if r.status_code in (403, 404) else ""
                raise NotionError(f"Notion {r.status_code}: {msg[:300]}{hint}", r.status_code)
            return r.json()
        raise NotionError(f"Notion is busy or failing ({r.status_code}); try again shortly.", r.status_code)

    # -- data sources -------------------------------------------------------------------
    async def get_data_source(self, ds_id: str) -> dict:
        return await self._request("GET", f"/data_sources/{clean_id(ds_id)}")

    async def get_database(self, db_id: str) -> dict:
        return await self._request("GET", f"/databases/{clean_id(db_id)}")

    async def query(self, ds_id: str, *, filter: dict | None = None, sorts: list | None = None, limit: int = 500) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            body: dict = {"page_size": min(100, limit - len(out))}
            if filter:
                body["filter"] = filter
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            data = await self._request("POST", f"/data_sources/{clean_id(ds_id)}/query", json=body)
            out += data.get("results") or []
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    # -- pages ----------------------------------------------------------------------------
    async def get_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{clean_id(page_id)}")

    async def create_page(self, ds_id: str, properties: dict, children: list[dict] | None = None) -> dict:
        body = {"parent": {"type": "data_source_id", "data_source_id": clean_id(ds_id)}, "properties": properties}
        if children:
            body["children"] = children[:100]
        page = await self._request("POST", "/pages", json=body)
        if children and len(children) > 100:
            await self.append_blocks(page["id"], children[100:])
        return page

    async def update_page(self, page_id: str, properties: dict) -> dict:
        return await self._request("PATCH", f"/pages/{clean_id(page_id)}", json={"properties": properties})

    # -- blocks ---------------------------------------------------------------------------
    async def get_blocks(self, block_id: str, *, limit: int = 300) -> list[dict]:
        out, cursor = [], None
        while len(out) < limit:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await self._request("GET", f"/blocks/{clean_id(block_id)}/children", params=params)
            out += data.get("results") or []
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out

    async def append_blocks(self, block_id: str, children: list[dict]) -> None:
        for i in range(0, len(children), 100):
            await self._request("PATCH", f"/blocks/{clean_id(block_id)}/children", json={"children": children[i:i + 100]})
