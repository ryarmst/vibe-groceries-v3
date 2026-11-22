"""PC Express (No Frills) retailer client.

Only an explicit set of operations is implemented: search, product details, deals, cart
read, cart quantity changes. There is deliberately no generic request method, and the
private transport refuses any path outside `_ALLOWED`, so checkout / order / payment /
timeslot endpoints cannot be reached through this client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from shopping.auth.session import AuthSession
from shopping.retailer.normalize import (
    Cart,
    NormalizedProduct,
    PastOrderData,
    PastOrderSummary,
    SubstitutionSpec,
    cart_from_response,
    from_product_detail,
    from_search_tile,
    past_order_from_detail,
    past_orders_from_history,
    substitute_text,
)

log = logging.getLogger(__name__)

# (method, path regex) pairs this client is permitted to call. Nothing else.
_ALLOWED: list[tuple[str, re.Pattern]] = [
    ("POST", re.compile(r"^/v2/products/search$")),
    ("GET", re.compile(r"^/v1/products/[\w-]+$")),
    ("GET", re.compile(r"^/v1/customers/[\w-]+/carts$")),
    ("GET", re.compile(r"^/v1/carts/[\w-]+$")),
    ("POST", re.compile(r"^/v1/carts/[\w-]+$")),  # body restricted to {"entries": ...} below
    ("GET", re.compile(r"^/v1/ecommerce/v2/[\w-]+/customers/historical-orders$")),  # order history (read-only)
    ("GET", re.compile(r"^/v1/ecommerce/v2/[\w-]+/customers/historical-orders/\d+$")),
]

DEAL_FILTERS = {
    "flyer": {"flyers": ["FLYER_ITEM"]},
    "price_reduction": {"promotions": ["Price Reduction"]},
}


class RetailerError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class RetailerUnavailable(RetailerError):
    """A No Frills backend service (pricing, inventory, ...) is down or timed out. Worth retrying later."""


_FRIENDLY = {
    "PRICING_PROMO_ENGINE_ERROR": "No Frills' pricing service",
    "INVENTORY_SERVICE_ERROR": "No Frills' inventory service",
}


def _classify_error(op: str, resp: httpx.Response) -> RetailerError:
    """Turn the retailer's nested error JSON into a readable exception.

    Cart errors look like {"errors":[{"message":"{\"error_response\":{\"message\":...,\"error_code\":...,
    \"details\":{\"status\":526}}}"}]} — a JSON string inside JSON."""
    code = message = None
    upstream_status = None
    try:
        data = resp.json()
    except ValueError:
        data = None
    errors = data.get("errors") if isinstance(data, dict) else data if isinstance(data, list) else None
    if isinstance(data, dict) and not errors and (data.get("message") or data.get("code")):
        errors = [data]  # flat {"code": ..., "message": ...}
    for e in errors or []:
        # Shapes seen: {"message": "<JSON string with error_response>"}, {"code":..,"message":..}, or a bare string.
        e = e if isinstance(e, dict) else {"message": e}
        inner = e.get("message")
        try:
            parsed = json.loads(inner) if isinstance(inner, str) else inner
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            inner = parsed.get("error_response", parsed)
        inner = inner if isinstance(inner, dict) else {"message": None if inner is None else str(inner)}
        code = inner.get("error_code") or inner.get("code") or e.get("code") or code
        message = inner.get("message") or e.get("description") or message
        details = inner.get("details")
        upstream_status = (details.get("status") if isinstance(details, dict) else None) or upstream_status
        break
    code = None if code is None else str(code)
    message = None if message is None else str(message)
    if not isinstance(upstream_status, int):
        upstream_status = int(upstream_status) if str(upstream_status or "").isdigit() else None
    upstream_down = (upstream_status or 0) >= 500 or "timeout" in resp.text.lower() or "unavailable" in resp.text.lower()
    transient = resp.status_code >= 500 or ((code or "").endswith("_ERROR") and upstream_down)
    if transient:
        what = _FRIENDLY.get(code or "", "A No Frills service")
        return RetailerUnavailable(f"{what} is unavailable right now ({code or resp.status_code}). Nothing was changed; try again in a few minutes.", code=code)
    return RetailerError(f"No Frills rejected {op}: {message or resp.text[:200]}" + (f" ({code})" if code else ""), code=code)


class PCExpressClient:
    MIN_INTERVAL_SECONDS = 0.25  # be polite to the retailer
    RETRY_DELAYS = (0, 1.5, 4)  # transient upstream failures are retried twice

    def __init__(self, *, api_base: str, api_key: str, banner: str, store_id: str, auth: AuthSession, raw_dir: Path | None = None):
        if not api_key:
            raise RetailerError("PCX_API_KEY is not configured (see .env.example).")
        self._base = api_base.rstrip("/")
        self._banner = banner
        self.store_id = store_id
        self._auth = auth
        self._raw_dir = raw_dir
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._cart_id: str | None = None
        self._cart_customer: str | None = None
        self._http = httpx.AsyncClient(
            timeout=30,
            headers={
                "x-apikey": api_key,
                "site-banner": banner,
                "basesiteid": banner,
                "x-loblaw-tenant-id": "ONLINE_GROCERIES",
                "business-user-agent": "PCXWEB",
                "x-application-type": "web",
                "x-channel": "web",
                "accept-language": "en",
                "is-iceberg-enabled": "true",
                "is-helios-account": "false",
                "accept": "application/json",
                "content-type": "application/json",
                "origin": f"https://www.{banner}.ca",
                "referer": f"https://www.{banner}.ca/",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- transport -------------------------------------------------------------------
    async def _call(self, op: str, method: str, path: str, *, params: dict | None = None, body: dict | None = None, auth: bool = False) -> dict:
        if not any(m == method and rx.match(path) for m, rx in _ALLOWED):
            raise RetailerError(f"Refusing non-allowlisted retailer call {method} {path}")
        headers = await self._auth.get_headers() if auth else {}
        last_error: RetailerError | None = None
        renewed = False
        for attempt, delay in enumerate(self.RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                resp = await self._send(op, method, path, params, body, headers)
            except httpx.TransportError as exc:
                last_error = RetailerUnavailable(f"No Frills didn't respond ({type(exc).__name__}).", code="NETWORK")
                log.warning("retailer op=%s transport error attempt=%s: %s", op, attempt + 1, exc)
                continue
            if resp.status_code in (401, 403):
                if auth and resp.status_code == 401 and not renewed:
                    renewed = True  # token revoked or expired early: renew once and retry right away
                    await self._auth.token_rejected()
                    headers = await self._auth.get_headers()
                    resp = await self._send(op, method, path, params, body, headers)
            if resp.status_code in (401, 403):
                if auth:
                    raise RetailerError(f"No Frills rejected the session ({resp.status_code}); sign in again under Settings → No Frills account.")
                raise RetailerError(f"Retailer refused the request ({resp.status_code}).")
            if resp.status_code >= 400:
                err = _classify_error(op, resp)
                if isinstance(err, RetailerUnavailable):
                    last_error = err
                    log.warning("retailer op=%s upstream unavailable attempt=%s code=%s", op, attempt + 1, err.code)
                    continue
                raise err
            data = resp.json()
            self._save_raw(op, data)
            return data
        raise last_error or RetailerError(f"Retailer call {op} failed.")

    async def _send(self, op: str, method: str, path: str, params, body, headers) -> httpx.Response:
        async with self._lock:
            wait = self.MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            started = time.monotonic()
            try:
                resp = await self._http.request(method, self._base + path, params=params, json=body, headers=headers)
            finally:
                self._last_call = time.monotonic()
        log.info("retailer op=%s %s %s status=%s duration_ms=%.0f", op, method, path, resp.status_code, (time.monotonic() - started) * 1000)
        return resp

    def _save_raw(self, op: str, data: dict) -> None:
        """Keep the latest raw response per operation for debugging (customer details stripped)."""
        if not self._raw_dir:
            return
        try:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            clean = data
            if isinstance(data, dict) and ("customer" in data or "cart" in data):
                clean = json.loads(json.dumps(data))
                (clean.get("cart") or clean).pop("customer", None)
            (self._raw_dir / f"{op}.json").write_text(json.dumps(clean))
        except Exception:  # noqa: BLE001 - debugging aid only
            log.debug("could not save raw response for %s", op)

    def _date(self) -> str:
        return datetime.now(ZoneInfo("America/Toronto")).strftime("%d%m%Y")

    # -- catalogue -------------------------------------------------------------------
    async def _search(self, term: str, filters: dict, page: int, page_size: int) -> tuple[list[NormalizedProduct], dict]:
        body = {
            "listingInfo": {"filters": filters, "includeFiltersInResponse": True, "pagination": {"from": page, "size": page_size}, "sort": {}},
            "banner": self._banner,
            "cart": {"cartId": ""},
            "userData": {},
            "fulfillmentInfo": {"offerType": "OG", "storeId": self.store_id, "pickupType": "STORE", "date": self._date(), "timeSlot": None},
            "searchRelatedInfo": {"term": term, "options": []},
            "device": {"screenSize": 1920},
        }
        data = await self._call("search", "POST", "/v2/products/search", body=body)
        products: list[NormalizedProduct] = []
        meta: dict = {}
        sections = (data.get("layout") or {}).get("sections") or {}
        for comp in (sections.get("mainContentCollection") or {}).get("components") or []:
            cdata = comp.get("data") or {}
            for tile in cdata.get("productTiles") or []:
                products.append(from_search_tile(tile))
            if cdata.get("pagination"):
                meta["total_results"] = cdata["pagination"].get("totalResults")
        for comp in (sections.get("filtersSection") or {}).get("components") or []:
            groups = (comp.get("data") or {}).get("filterGroups")
            if groups:
                meta["filter_groups"] = groups
        return products, meta

    async def search_products(self, query: str, *, page: int = 1, include_sponsored: bool = False, filters: dict | None = None) -> dict:
        products, meta = await self._search(query, filters or {}, page, 48)
        if not include_sponsored:
            products = [p for p in products if not p.sponsored]
        # Sponsored placements can duplicate organic results.
        seen: set[str] = set()
        unique = []
        for p in products:
            if p.retailer_product_id not in seen:
                seen.add(p.retailer_product_id)
                unique.append(p)
        return {"products": unique, "total_results": meta.get("total_results"), "filter_groups": meta.get("filter_groups", [])}

    async def get_product(self, product_id: str) -> NormalizedProduct:
        params = {"lang": "en", "date": self._date(), "pickupType": "STORE", "storeId": self.store_id, "banner": self._banner}
        data = await self._call("product", "GET", f"/v1/products/{product_id}", params=params)
        return from_product_detail(data)

    async def get_deals(self, *, query: str = "", kind: str = "flyer", category_code: str | None = None, min_discount_pct: float | None = None, limit: int = 50) -> list[NormalizedProduct]:
        if kind not in DEAL_FILTERS:
            raise RetailerError(f"Unknown deal kind {kind!r}; use one of {sorted(DEAL_FILTERS)}")
        filters = dict(DEAL_FILTERS[kind])
        if category_code:
            filters["category"] = [category_code]
        out: list[NormalizedProduct] = []
        seen: set[str] = set()
        page = 1
        while len(out) < limit and page <= 10:
            products, meta = await self._search(query, filters, page, 48)
            if not products:
                break
            for p in products:
                if p.sponsored or p.retailer_product_id in seen:
                    continue
                seen.add(p.retailer_product_id)
                if min_discount_pct is not None and (p.discount_pct or 0) < min_discount_pct:
                    continue
                out.append(p)
            total = meta.get("total_results") or 0
            if page * 48 >= total:
                break
            page += 1
        return out[:limit]

    # -- cart (authenticated) --------------------------------------------------------
    async def _current_cart_id(self) -> str:
        customer_id = await self._auth.ensure_authenticated()
        # Cache per customer: pasting a different account's token must never reuse the old cart.
        if self._cart_id and self._cart_customer == customer_id:
            return self._cart_id
        data = await self._call("customer_carts", "GET", f"/v1/customers/{customer_id}/carts", params={"status": "open", "banner": self._banner}, auth=True)
        carts = data.get("carts") or []
        if not carts:
            raise RetailerError("No open cart on the account. Open nofrills.ca once while logged in to create one.")
        self._cart_id, self._cart_customer = carts[0]["id"], customer_id
        return self._cart_id

    async def get_cart(self) -> Cart:
        cart_id = await self._current_cart_id()
        try:
            data = await self._call("cart", "GET", f"/v1/carts/{cart_id}", auth=True)
        except RetailerError:
            self._cart_id = None  # cart may have been replaced; rediscover next time
            raise
        return cart_from_response(data)

    async def set_cart_quantity(self, product_id: str, quantity: float, *, substitution: SubstitutionSpec | None = None) -> Cart:
        """Set an item's quantity (absolute). With `substitution`, also set its substitution preference."""
        if quantity < 0:
            raise RetailerError("Quantity cannot be negative.")
        cart_id = await self._current_cart_id()
        entry: dict = {"quantity": quantity, "fulfillmentMethod": "pickup", "sellerId": self.store_id}
        if substitution is not None:
            entry.update({
                "substitutionComment": (substitution.comment or "")[:250],
                "substitutionAllowed": bool(substitution.allowed or substitution.substitute),
                "selectedSubstitution": substitute_text(substitution.substitute) if substitution.substitute else "",
            })
        try:
            data = await self._call("cart_update", "POST", f"/v1/carts/{cart_id}", body={"entries": {product_id: entry}}, auth=True)
        except RetailerError as exc:
            # Substitution fields only apply to items already in the cart: for a new item No Frills answers
            # ITEM_NOT_FOUND. Do what the website does — add it, then set the substitution.
            if substitution is None or quantity == 0 or exc.code != "ITEM_NOT_FOUND":
                raise
            await self.set_cart_quantity(product_id, quantity)
            try:
                data = await self._call("cart_update", "POST", f"/v1/carts/{cart_id}", body={"entries": {product_id: entry}}, auth=True)
            except RetailerError as exc2:
                raise RetailerError(f"Added to the cart, but the substitution setting wasn't saved: {exc2}", code=exc2.code) from exc2
        errors = (data.get("errors") if isinstance(data, dict) else None) or []
        if errors:
            # e.g. [{"code": "0014", "message": "Product is out of stock.", ...}]
            first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
            text = "; ".join(str(e.get("message") or e.get("code")) if isinstance(e, dict) else str(e) for e in errors)
            raise RetailerError(f"No Frills didn't add it: {text[:300]}", code=str(first.get("code")) if first.get("code") else None)
        return cart_from_response(data)

    async def set_cart_substitution(self, product_id: str, substitution: SubstitutionSpec) -> Cart:
        """Change only the substitution preference of an item already in the cart."""
        cart = await self.get_cart()
        line = next((l for l in cart.lines if l.product_id == product_id), None)
        if line is None:
            raise RetailerError("That item isn't in the cart.")
        return await self.set_cart_quantity(product_id, line.quantity, substitution=substitution)

    async def remove_cart_item(self, product_id: str) -> Cart:
        return await self.set_cart_quantity(product_id, 0)

    # -- order history (authenticated, read-only) ---------------------------------------
    async def list_past_orders(self) -> list[PastOrderSummary]:
        await self._auth.ensure_authenticated()
        data = await self._call("past_orders", "GET", f"/v1/ecommerce/v2/{self._banner}/customers/historical-orders", auth=True)
        return past_orders_from_history(data)

    async def get_past_order(self, order_id: str) -> PastOrderData:
        if not str(order_id).isdigit():
            raise RetailerError("Order numbers are digits only.")
        await self._auth.ensure_authenticated()
        data = await self._call("past_order", "GET", f"/v1/ecommerce/v2/{self._banner}/customers/historical-orders/{order_id}", auth=True)
        return past_order_from_detail(data)
