"""Normalize PC Express product data into a single comparable representation.

Observed `packageSizing` formats (search tiles), e.g.:
    "1 kg, $0.60/100g"            single package, weight
    "4 l, $0.16/100ml"            single package, volume
    "4x300.0 ml, $0.58/100ml"     multipack
    "12 ea, $0.33/1ea"            count
    "$15.41/1kg $6.99/1lb"        sold each but priced by weight (meat, some produce)
    "N g, $N/Nkg $N/Ng"           mixed
Pricing types (`pricingUnits.type`): SOLD_BY_EACH, SOLD_BY_EACH_PRICED_BY_WEIGHT, SOLD_BY_WEIGHT.

Prices are kept as Decimal internally. Unit prices use a fixed basis per family:
weight -> per 100 g, volume -> per 100 ml, count -> per 1 ea. Weight and volume are never
compared with each other.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FAMILY_BASIS = {"weight": ("100g", Decimal(100)), "volume": ("100ml", Decimal(100)), "count": ("1ea", Decimal(1))}

# unit -> (family, multiplier to canonical unit g / ml / ea)
_UNITS: dict[str, tuple[str, Decimal]] = {
    "mg": ("weight", Decimal("0.001")),
    "g": ("weight", Decimal(1)),
    "kg": ("weight", Decimal(1000)),
    "lb": ("weight", Decimal("453.59237")),
    "oz": ("weight", Decimal("28.349523125")),
    "ml": ("volume", Decimal(1)),
    "l": ("volume", Decimal(1000)),
    "ea": ("count", Decimal(1)),
    "un": ("count", Decimal(1)),
    "pk": ("count", Decimal(1)),
    "ct": ("count", Decimal(1)),
}
_CANON = {"weight": "g", "volume": "ml", "count": "ea"}

_SIZE_RE = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(mg|kg|g|ml|l|ea|un|pk|ct|lb|oz)\b", re.I)
_UNIT_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)?\s*(mg|kg|g|ml|l|ea|un|lb|oz)\b", re.I)
_MONEY_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def money(value: Any) -> Decimal | None:
    """Parse "$6.29", "5.97", 5.97 -> Decimal. None/empty -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    m = _MONEY_RE.search(str(value))
    if not m:
        return None
    try:
        return Decimal(m.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def round2(value: Decimal | None) -> float | None:
    return None if value is None else float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class ParsedSize:
    raw: str | None
    value: Decimal | None  # total quantity in canonical unit (g / ml / ea)
    unit: str | None
    family: str  # weight | volume | count | unknown
    pack_count: int | None = None


def parse_size(text: str | None) -> ParsedSize:
    """Parse the leading package size of a sizing string ("4x300.0 ml, ..." -> 1200 ml)."""
    if not text:
        return ParsedSize(None, None, None, "unknown")
    head = text.split(",")[0].strip()
    m = _SIZE_RE.match(head)
    if not m:
        return ParsedSize(None, None, None, "unknown")
    count = Decimal(m.group(1)) if m.group(1) else Decimal(1)
    qty = Decimal(m.group(2))
    family, mult = _UNITS[m.group(3).lower()]
    # "3x98.0 ea" is 3 rolls of 98 sheets; compare count items per outer unit (roll/box).
    total = count if (family == "count" and m.group(1)) else count * qty * mult
    return ParsedSize(head, total.normalize(), _CANON[family], family, int(count) if m.group(1) else None)


@dataclass
class RetailerUnitPrice:
    text: str
    family: str
    per_basis: Decimal  # price per our basis for that family


def parse_retailer_unit_prices(text: str | None) -> list[RetailerUnitPrice]:
    """All "$X/Nunit" fragments, converted to our basis (per 100 g, per 100 ml, per 1 ea)."""
    out: list[RetailerUnitPrice] = []
    for m in _UNIT_PRICE_RE.finditer(text or ""):
        price = Decimal(m.group(1))
        qty = Decimal(m.group(2)) if m.group(2) else Decimal(1)
        family, mult = _UNITS[m.group(3).lower()]
        canonical_qty = qty * mult
        if canonical_qty == 0:
            continue
        per_basis = price / canonical_qty * FAMILY_BASIS[family][1]
        out.append(RetailerUnitPrice(m.group(0), family, per_basis))
    return out


@dataclass
class NormalizedProduct:
    retailer_product_id: str
    name: str
    brand: str | None = None
    article_number: str | None = None
    uom: str | None = None
    gtin: str | None = None
    raw_size: str | None = None
    raw_package_sizing: str | None = None
    size_value: Decimal | None = None
    size_unit: str | None = None
    size_estimated: bool = False
    comparison_family: str = "unknown"
    sold_by: str = "each"  # each | each_priced_by_weight | weight
    regular_price: Decimal | None = None
    sale_price: Decimal | None = None
    effective_price: Decimal | None = None
    member_price: Decimal | None = None
    unit_price: Decimal | None = None
    unit_price_basis: str | None = None
    retailer_unit_price: str | None = None
    deal_type: str | None = None
    deal_text: str | None = None
    deal_expiry: str | None = None
    pc_optimum_text: str | None = None
    promotion_text: str | None = None
    on_flyer: bool | None = None
    availability: str | None = None
    sponsored: bool = False
    min_quantity: float | None = None
    max_quantity: float | None = None
    quantity_interval: float | None = None
    image_url: str | None = None
    link: str | None = None
    categories: list = field(default_factory=list)  # [(code, name), ...] root first, e.g. Food > Meat > Beef
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def discount_amount(self) -> Decimal | None:
        if self.regular_price is not None and self.effective_price is not None and self.regular_price > self.effective_price:
            return self.regular_price - self.effective_price
        return None

    @property
    def discount_pct(self) -> float | None:
        d = self.discount_amount
        if d is None or not self.regular_price:
            return None
        return round(float(d / self.regular_price * 100), 1)

    def summary(self) -> dict:
        """Compact JSON-safe view for MCP / UI (no raw payload, rounded money)."""
        return {
            "product_id": self.retailer_product_id,
            "name": self.name,
            "brand": self.brand,
            "package_size": self.raw_size,
            "size": None if self.size_value is None else float(self.size_value),
            "size_unit": self.size_unit,
            "size_estimated": self.size_estimated or None,
            "comparison_family": self.comparison_family,
            "sold_by": self.sold_by,
            "price": round2(self.effective_price),
            "regular_price": round2(self.regular_price),
            "sale_price": round2(self.sale_price),
            "member_price": round2(self.member_price),
            "discount_pct": self.discount_pct,
            "unit_price": None if self.unit_price is None else round(float(self.unit_price), 4),
            "unit_price_basis": self.unit_price_basis,
            "deal": self.deal_text,
            "deal_type": self.deal_type,
            "deal_expiry": self.deal_expiry,
            "pc_optimum": self.pc_optimum_text,
            "promotion": self.promotion_text,
            "availability": self.availability,
            "sponsored": self.sponsored or None,
            "max_quantity": self.max_quantity,
            "category": " > ".join(n for _, n in self.categories) or None,
        }

    def to_record(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


def _sold_by(pricing_type: str | None) -> str:
    return {
        "SOLD_BY_EACH_PRICED_BY_WEIGHT": "each_priced_by_weight",
        "SOLD_BY_WEIGHT": "weight",
    }.get(pricing_type or "", "each")


def _finish_pricing(p: NormalizedProduct, sizing_text: str | None, pricing_interval: float | None) -> None:
    """Compute size family + unit price from sizing text and effective price."""
    size = parse_size(sizing_text)
    retailer_prices = parse_retailer_unit_prices(sizing_text)
    p.raw_size, p.size_value, p.size_unit, p.comparison_family = size.raw, size.value, size.unit, size.family
    if retailer_prices:
        p.retailer_unit_price = " ".join(rp.text for rp in retailer_prices)
    if p.comparison_family == "unknown" and retailer_prices:
        p.comparison_family = retailer_prices[0].family
        p.size_unit = _CANON[p.comparison_family]

    family = p.comparison_family
    if family == "unknown":
        return
    basis_label, basis_qty = FAMILY_BASIS[family]
    p.unit_price_basis = basis_label
    same_family = [rp for rp in retailer_prices if rp.family == family]

    if p.sold_by == "weight" and same_family:
        # Price is per selling interval (e.g. per 100 g); retailer $/kg is the true unit price.
        p.unit_price = same_family[0].per_basis
    elif p.sold_by == "each_priced_by_weight" and same_family:
        # Displayed price is an estimate for an average pack; $/kg is authoritative.
        p.unit_price = same_family[0].per_basis
        if p.effective_price is not None and p.unit_price:
            p.size_value = (p.effective_price / p.unit_price * basis_qty).quantize(Decimal("1"))
            p.size_estimated = True
    elif p.size_value and p.effective_price is not None:
        p.unit_price = p.effective_price / p.size_value * basis_qty
    elif same_family:
        p.unit_price = same_family[0].per_basis


def from_search_tile(tile: dict) -> NormalizedProduct:
    pricing = tile.get("pricing") or {}
    units = tile.get("pricingUnits") or {}
    deal = tile.get("deal") if isinstance(tile.get("deal"), dict) else None
    price = money(pricing.get("price"))
    was = money(pricing.get("wasPrice"))
    member = money(pricing.get("memberOnlyPrice"))
    promos = tile.get("promotions") or []
    promo_texts = [x.get("text") for x in promos if isinstance(x, dict) and x.get("text")]
    pco = tile.get("pcoDeal")
    mop = tile.get("mopDeal")
    images = tile.get("productImage") or []
    p = NormalizedProduct(
        retailer_product_id=tile.get("productId") or tile.get("code") or "",
        name=(tile.get("title") or "").strip(),
        brand=(tile.get("brand") or None),
        article_number=tile.get("articleNumber"),
        uom=tile.get("uom"),
        raw_package_sizing=tile.get("packageSizing"),
        sold_by=_sold_by(units.get("type")),
        regular_price=was if was is not None else price,
        sale_price=price if was is not None else None,
        effective_price=price,
        member_price=member,
        deal_type=deal.get("type") if deal else None,
        deal_text=deal.get("text") if deal else None,
        deal_expiry=deal.get("expiryDate") if deal else None,
        pc_optimum_text=pco if isinstance(pco, str) else (pco.get("text") if isinstance(pco, dict) else None),
        promotion_text="; ".join(t for t in [*(promo_texts), (mop if isinstance(mop, str) else None)] if t) or None,
        availability=(tile.get("inventoryIndicator") or {}).get("text") if isinstance(tile.get("inventoryIndicator"), dict) else None,
        sponsored=bool(tile.get("isSponsored")),
        min_quantity=units.get("minOrderQuantity"),
        max_quantity=units.get("maxOrderQuantity"),
        quantity_interval=units.get("interval"),
        image_url=(images[0].get("smallUrl") or images[0].get("imageUrl")) if images else None,
        link=tile.get("link"),
        raw=tile,
    )
    _finish_pricing(p, tile.get("packageSizing"), units.get("interval"))
    return p


def from_product_detail(prod: dict) -> NormalizedProduct:
    offers = prod.get("offers") or [{}]
    offer = offers[0] or {}
    units = prod.get("pricingUnits") or {}
    price_obj = offer.get("price") or {}
    was_obj = offer.get("wasPrice") or {}
    price = money(price_obj.get("value"))
    was = money(was_obj.get("value"))
    special = price_obj.get("type") == "SPECIAL"
    comparison = " ".join(
        f"${c['value']}/{c.get('quantity') or 1}{c.get('unit')}" for c in (offer.get("comparisonPrices") or []) if c.get("value") is not None
    )
    sizing = ", ".join(x for x in [prod.get("packageSize"), comparison] if x)
    promos = offer.get("promotions") or []
    gtin = prod.get("gtin")
    if isinstance(gtin, list):
        gtin = ",".join(str(g) for g in gtin) or None
    images = prod.get("imageAssets") or []
    p = NormalizedProduct(
        retailer_product_id=prod.get("code") or "",
        name=(prod.get("name") or "").strip(),
        brand=prod.get("brand"),
        article_number=prod.get("articleNumber"),
        uom=prod.get("uom"),
        gtin=gtin,
        raw_package_sizing=sizing or None,
        sold_by=_sold_by(units.get("type")),
        regular_price=was if (special and was is not None) else price,
        sale_price=price if special else None,
        effective_price=price,
        member_price=money((offer.get("memberOnlyPrice") or {}).get("value")) if isinstance(offer.get("memberOnlyPrice"), dict) else money(offer.get("memberOnlyPrice")),
        deal_type="SALE" if special else None,
        deal_expiry=price_obj.get("expiryDate") if special else None,
        promotion_text="; ".join(x.get("text") or x.get("description") or "" for x in promos if isinstance(x, dict)) or None,
        availability=offer.get("stockStatus"),
        min_quantity=units.get("minOrderQuantity"),
        max_quantity=units.get("maxOrderQuantity"),
        quantity_interval=units.get("interval"),
        image_url=(images[0].get("smallUrl") or images[0].get("imageUrl")) if images else None,
        link=prod.get("link"),
        categories=[(str(b["categoryCode"]), b.get("name") or "") for b in (prod.get("breadcrumbs") or [])
                    if isinstance(b, dict) and b.get("categoryCode")],
        raw=prod,
    )
    if special and was is not None and price is not None and was > price:
        p.deal_text = f"SAVE ${(was - price):.2f}"
    _finish_pricing(p, sizing, units.get("interval"))
    return p


@dataclass
class CartLine:
    product_id: str
    name: str
    brand: str | None
    quantity: float
    unit_price: float | None
    line_total: float | None
    regular_line_total: float | None
    selling_type: str | None
    seller_id: str | None
    substitution_allowed: bool | None = None
    substitution_comment: str | None = None
    selected_substitution: str | None = None  # retailer product id of a chosen substitute


@dataclass
class Cart:
    cart_id: str
    store_id: str | None
    store_name: str | None
    lines: list[CartLine]
    subtotal: float | None
    estimated_total: float | None
    total_discounts: float | None

    def summary(self) -> dict:
        return {
            "cart_id": self.cart_id,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "items": [asdict(line) for line in self.lines],
            "item_count": len(self.lines),
            "subtotal": self.subtotal,
            "estimated_total": self.estimated_total,
            "total_discounts": self.total_discounts,
        }

    def quantity_of(self, product_id: str) -> float:
        for line in self.lines:
            if line.product_id == product_id:
                return line.quantity
        return 0.0


@dataclass
class SubstitutionSpec:
    """Per-item substitution instructions sent with a cart update."""

    allowed: bool
    comment: str = ""
    substitute: NormalizedProduct | None = None  # a specific replacement the shopper should use


def substitute_text(p: NormalizedProduct) -> str:
    """The retailer's format for a customer-chosen substitute (as sent by the website)."""
    price = f"{p.effective_price:.2f}" if p.effective_price is not None else ""
    return f"Customer Substitute: {p.brand or ''}, {p.name}, Price: {price}, ArticleID: {p.retailer_product_id}"


def _obj(value) -> dict:
    """Nested retailer fields are usually objects but sometimes a bare id/string or null."""
    return value if isinstance(value, dict) else {}


def cart_from_response(data: dict) -> Cart:
    data = _obj(data)
    cart = _obj(data.get("cart")) or data
    lines: list[CartLine] = []
    store_id = store_name = None
    subtotal = est = disc = None
    for order in cart.get("orders") or []:
        order = _obj(order)
        pickup = _obj(_obj(_obj(order.get("fulfillment")).get("pickupBooking")).get("pickupLocation"))
        store_id = store_id or pickup.get("storeId") or pickup.get("id")
        store_name = store_name or pickup.get("name")
        totals = _obj(order.get("totals"))
        subtotal = totals.get("subTotal", subtotal)
        est = totals.get("estimatedTotal", est)
        disc = totals.get("totalDiscounts", disc)
        for entry in order.get("entries") or []:
            entry = _obj(entry)
            offer = _obj(entry.get("offer"))
            product = _obj(offer.get("product"))
            prices = _obj(entry.get("prices"))
            vendor = offer.get("vendor")
            subs = entry.get("substitutionPreference")
            subs = subs if isinstance(subs, dict) else {"substitutionAllowed": subs} if isinstance(subs, bool) else {}
            selected = entry.get("selectedSubstitution")
            if isinstance(selected, dict):
                selected = selected.get("id") or selected.get("code")
            lines.append(
                CartLine(
                    product_id=product.get("id") or offer.get("id") or "",
                    name=product.get("name") or "",
                    brand=product.get("brand"),
                    quantity=float(entry.get("quantity") or 0),
                    unit_price=prices.get("salePrice"),
                    line_total=prices.get("totalSalePrice"),
                    regular_line_total=prices.get("totalRegularPrice"),
                    selling_type=offer.get("sellingType"),
                    seller_id=vendor.get("id") if isinstance(vendor, dict) else vendor,
                    substitution_allowed=subs.get("substitutionAllowed"),
                    substitution_comment=subs.get("comment") or None,
                    selected_substitution=selected or None,
                )
            )
    return Cart(cart.get("id") or "", store_id, store_name, lines, subtotal, est, disc)


# ---------------------------------------------------------------------------------------
# Past orders (order history)
# ---------------------------------------------------------------------------------------

_FEE_RE = re.compile(r"\b(TIP|PD ORDER|WAIVE|FEE|CHARGE)\b")


@dataclass
class PastOrderSummary:
    order_id: str
    placed_at: str | None
    total: float | None
    store: str | None
    fulfillment: str | None
    order_type: str | None


@dataclass
class PastOrderLineData:
    product_id: str
    name: str
    brand: str | None
    size: str | None
    quantity: float | None
    weight_kg: float | None
    unit_price: Decimal | None  # per item, or per kg for weighed items
    total: Decimal | None
    fulfilled: bool
    is_fee: bool
    weighted: bool
    image_url: str | None = None
    link: str | None = None
    substitution_allowed: bool | None = None
    substituted_with: str | None = None


@dataclass
class PastOrderData:
    order_id: str
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    points_earned: float | None
    lines: list[PastOrderLineData]


def past_orders_from_history(data: dict) -> list[PastOrderSummary]:
    return [
        PastOrderSummary(order_id=str(o.get("id")), placed_at=o.get("placed"), total=o.get("total"), store=o.get("store"),
                         fulfillment=o.get("fulfillmentType"), order_type=o.get("orderType"))
        for o in data.get("orderHistory") or [] if o.get("id")
    ]


def past_order_from_detail(data: dict) -> PastOrderData:
    od = data.get("orderDetails") or {}
    lines: list[PastOrderLineData] = []
    for e in od.get("entries") or []:
        p = e.get("product") or {}
        name = (p.get("productName") or p.get("name") or "").strip()
        weighted = bool(p.get("isWeighted") or p.get("weighted") or e.get("weight"))
        unit = money(e.get("unitPrice"))
        total = money(e.get("totalPrice"))
        qty = e.get("quantity") or 0
        weight = e.get("weight") or 0
        if weighted:
            quantity, weight_kg = None, (float(weight) or None)
        else:
            # A few lines report quantity 0 but still bill; infer the count from the line total.
            quantity = float(qty) if qty else (float(round(total / unit)) if unit and total else None)
            weight_kg = None
        fulfilled = not (not qty and not weight and not total)
        lines.append(PastOrderLineData(
            product_id=p.get("id") or "", name=name, brand=p.get("brand") or None, size=p.get("productTitleSizeDetail") or None,
            quantity=quantity, weight_kg=weight_kg, unit_price=unit, total=total, fulfilled=fulfilled,
            is_fee=bool(name.isupper() and _FEE_RE.search(name)), weighted=weighted,
            image_url=p.get("primaryImage"), link=p.get("link"),
            substitution_allowed=(e.get("substitutionData") or e.get("substitutionPreference") or {}).get("substitutionAllowed"),
            substituted_with=e.get("selectedSubstitution") or None,
        ))
    return PastOrderData(order_id=str(od.get("orderNumber") or ""), subtotal=money(od.get("subTotal")), tax=money(od.get("totalTax")),
                         total=money(od.get("totalPriceWithTax") or od.get("totalPrice")), points_earned=data.get("pointsEarned"), lines=lines)


def observation_from_order_line(line: PastOrderLineData) -> NormalizedProduct | None:
    """What was actually paid, as a price observation (skips fees and unfulfilled lines)."""
    if line.is_fee or not line.fulfilled or not line.product_id:
        return None
    if line.weighted:
        p = NormalizedProduct(retailer_product_id=line.product_id, name=line.name, brand=line.brand, sold_by="each_priced_by_weight",
                              effective_price=line.total, regular_price=line.total, image_url=line.image_url, link=line.link)
        if line.unit_price:
            _finish_pricing(p, f"${line.unit_price}/1kg", None)
        if line.weight_kg:
            p.size_value, p.size_unit, p.size_estimated = (Decimal(str(line.weight_kg)) * 1000).quantize(Decimal("1")), "g", False
        return p
    p = NormalizedProduct(retailer_product_id=line.product_id, name=line.name, brand=line.brand, effective_price=line.unit_price,
                          regular_price=line.unit_price, image_url=line.image_url, link=line.link)
    _finish_pricing(p, line.size, None)
    return p
