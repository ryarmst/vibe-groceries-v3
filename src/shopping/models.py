"""SQLAlchemy models. One SQLite database holds products, prices, runs, preferences and cart history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class DecimalText(TypeDecorator):
    """Exact Decimal storage (SQLite NUMERIC would round-trip through float)."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else str(Decimal(str(value)))

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class UTCDateTime(TypeDecorator):
    """Store naive UTC in SQLite, always hand back timezone-aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return None if value is None else value.replace(tzinfo=timezone.utc)


class JSONList(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return json.dumps(list(value or []))

    def process_result_value(self, value, dialect):
        return json.loads(value) if value else []


class Base(DeclarativeBase):
    pass


# Run lifecycle
RUN_BUILDING, RUN_REVIEW, RUN_PARTIAL, RUN_APPROVED, RUN_APPLIED, RUN_CANCELLED = (
    "building", "review", "partially_approved", "approved", "applied", "cancelled",
)
# Selection lifecycle
SEL_PENDING, SEL_APPROVED, SEL_REJECTED, SEL_APPLIED, SEL_FAILED = "pending", "approved", "rejected", "applied", "failed"

REASON_CODES = {
    "grocery_list": "Grocery List",
    "pantry_low": "Pantry Low",
    "regular_staple": "Regular Staple",
    "good_deal": "Good Deal",
    "preferred_product": "Preferred Product",
    "preferred_brand": "Preferred Brand",
    "user_requested": "User Requested",
    "alternative": "Alternative",
}

class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    retailer_product_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    article_number: Mapped[str | None] = mapped_column(String(32))
    uom: Mapped[str | None] = mapped_column(String(16))
    gtin: Mapped[str | None] = mapped_column(String(128))
    brand: Mapped[str | None] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(256))
    raw_size: Mapped[str | None] = mapped_column(String(64))
    raw_package_sizing: Mapped[str | None] = mapped_column(String(128))
    size_value: Mapped[Decimal | None] = mapped_column(DecimalText)
    size_unit: Mapped[str | None] = mapped_column(String(8))
    size_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    comparison_family: Mapped[str] = mapped_column(String(16), default="unknown")
    sold_by: Mapped[str] = mapped_column(String(32), default="each")
    image_url: Mapped[str | None] = mapped_column(String(512))
    link: Mapped[str | None] = mapped_column(String(512))
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Where it sits in the store (No Frills' own aisle tree): leaf category, full path, and the aisle
    # (second level under Food, e.g. "Meat"; first level elsewhere, e.g. "Household Supplies").
    category_code: Mapped[str | None] = mapped_column(String(16), index=True)
    category_path: Mapped[str | None] = mapped_column(String(256))
    aisle: Mapped[str | None] = mapped_column(String(64), index=True)
    # Latest known shelf price and deal (denormalized from the newest observation) for fast catalogue queries.
    current_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    current_regular_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    current_unit_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    current_unit_basis: Mapped[str | None] = mapped_column(String(16))
    current_deal_text: Mapped[str | None] = mapped_column(String(128))
    current_deal_expiry: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD, last day of the sale
    current_on_sale: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    current_availability: Mapped[str | None] = mapped_column(String(64))
    current_observed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)

    observations: Mapped[list[PriceObservation]] = relationship(back_populates="product", order_by="PriceObservation.observed_at")


class PriceObservation(Base):
    __tablename__ = "price_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    store_id: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(32))  # search | deals | product | selection | cart
    regular_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    sale_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    effective_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    member_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    raw_package_size: Mapped[str | None] = mapped_column(String(64))
    raw_package_sizing: Mapped[str | None] = mapped_column(String(128))
    normalized_quantity: Mapped[Decimal | None] = mapped_column(DecimalText)
    normalized_unit: Mapped[str | None] = mapped_column(String(8))
    size_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_price: Mapped[Decimal | None] = mapped_column(DecimalText)
    unit_price_basis: Mapped[str | None] = mapped_column(String(16))
    retailer_unit_price: Mapped[str | None] = mapped_column(String(64))
    deal_type: Mapped[str | None] = mapped_column(String(32))
    deal_text: Mapped[str | None] = mapped_column(String(128))
    deal_expiry: Mapped[str | None] = mapped_column(String(32))
    pc_optimum_text: Mapped[str | None] = mapped_column(String(128))
    promotion_text: Mapped[str | None] = mapped_column(String(256))
    availability: Mapped[str | None] = mapped_column(String(64))

    product: Mapped[Product] = relationship(back_populates="observations")


class ShoppingRun(Base):
    __tablename__ = "shopping_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(24), default=RUN_BUILDING)
    store_id: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str] = mapped_column(String(256), default="")
    notes: Mapped[str | None] = mapped_column(Text)

    selections: Mapped[list[Selection]] = relationship(back_populates="run", order_by="Selection.position")
    choice_groups: Mapped[list[ChoiceGroup]] = relationship(back_populates="run", order_by="ChoiceGroup.position")


class ChoiceGroup(Base):
    __tablename__ = "choice_groups"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shopping_run_id: Mapped[str] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    label: Mapped[str] = mapped_column(String(128))
    position: Mapped[int] = mapped_column(Integer, default=0)
    recommended_selection_id: Mapped[str | None] = mapped_column(String(16))
    selected_selection_id: Mapped[str | None] = mapped_column(String(16))

    run: Mapped[ShoppingRun] = relationship(back_populates="choice_groups")
    selections: Mapped[list[Selection]] = relationship(back_populates="choice_group", order_by="Selection.position")


class Selection(Base):
    __tablename__ = "selections"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    shopping_run_id: Mapped[str] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    choice_group_id: Mapped[str | None] = mapped_column(ForeignKey("choice_groups.id"), index=True)
    price_observation_id: Mapped[int | None] = mapped_column(ForeignKey("price_observations.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    position: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float, default=1)
    suggested_quantity: Mapped[float | None] = mapped_column(Float)  # what the agent proposed, before user edits
    added_by: Mapped[str] = mapped_column(String(16), default="agent")  # agent | user
    substitution_allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # may the shopper substitute?
    substitution_note: Mapped[str | None] = mapped_column(Text)  # instructions for the shopper ("any brand, same size")
    substitute_product_id: Mapped[str | None] = mapped_column(String(64))  # a specific replacement, if chosen
    status: Mapped[str] = mapped_column(String(16), default=SEL_PENDING, index=True)
    requirement: Mapped[str | None] = mapped_column(String(128))
    rationale: Mapped[str | None] = mapped_column(Text)
    reason_codes: Mapped[list] = mapped_column(JSONList, default=list)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    applied_to_cart_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ShoppingRun] = relationship(back_populates="selections")
    product: Mapped[Product] = relationship()
    choice_group: Mapped[ChoiceGroup | None] = relationship(back_populates="selections")
    observation: Mapped[PriceObservation | None] = relationship()


class CartAction(Base):
    __tablename__ = "cart_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    selection_id: Mapped[str | None] = mapped_column(ForeignKey("selections.id"))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))
    retailer_product_id: Mapped[str] = mapped_column(String(64))
    product_label: Mapped[str | None] = mapped_column(String(256))  # brand + name + size at the time
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(32))  # set_quantity | remove
    source: Mapped[str | None] = mapped_column(String(32))  # approve | apply | cart_page | modify | quick_shop | deals
    quantity_before: Mapped[float | None] = mapped_column(Float)
    quantity_after: Mapped[float | None] = mapped_column(Float)
    price_at_action: Mapped[Decimal | None] = mapped_column(DecimalText)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    upstream_reference: Mapped[str | None] = mapped_column(String(64))  # retailer cart id


class Feedback(Base):
    """A triage decision with the user's reason, kept for later pattern review."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    shopping_run_id: Mapped[str | None] = mapped_column(ForeignKey("shopping_runs.id"), index=True)
    selection_id: Mapped[str | None] = mapped_column(ForeignKey("selections.id"), index=True)
    choice_group_id: Mapped[str | None] = mapped_column(String(16))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)  # the item decided on
    chosen_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"))  # for chose_alternative
    requirement: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(32), index=True)  # removed | chose_alternative | approved | quantity_changed | note
    note: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)  # JSON snapshot: prices, agent reasons, options
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution: Mapped[str | None] = mapped_column(Text)  # what was done about it when reviewed
    device_id: Mapped[str | None] = mapped_column(String(32), index=True)  # who wrote it (a User id)

    product: Mapped[Product | None] = relationship(foreign_keys=[product_id])
    chosen_product: Mapped[Product | None] = relationship(foreign_keys=[chosen_product_id])


class PastOrder(Base):
    """A placed order imported from the retailer's order history — what was actually bought."""

    __tablename__ = "past_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # retailer order number
    placed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    store: Mapped[str | None] = mapped_column(String(128))
    fulfillment: Mapped[str | None] = mapped_column(String(32))
    subtotal: Mapped[Decimal | None] = mapped_column(DecimalText)
    tax: Mapped[Decimal | None] = mapped_column(DecimalText)
    total: Mapped[Decimal | None] = mapped_column(DecimalText)
    points_earned: Mapped[float | None] = mapped_column(Float)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    lines: Mapped[list[PastOrderLine]] = relationship(back_populates="order", order_by="PastOrderLine.position", cascade="all, delete-orphan")


class PastOrderLine(Base):
    __tablename__ = "past_order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("past_orders.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    retailer_product_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256))
    brand: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[str | None] = mapped_column(String(64))
    quantity: Mapped[float | None] = mapped_column(Float)  # count, for items sold each
    weight_kg: Mapped[float | None] = mapped_column(Float)  # for weighed items
    unit_price: Mapped[Decimal | None] = mapped_column(DecimalText)  # per item, or per kg when weighed
    total: Mapped[Decimal | None] = mapped_column(DecimalText)
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=True)  # False: billed nothing (unavailable)
    is_fee: Mapped[bool] = mapped_column(Boolean, default=False)  # delivery fee / tip pseudo-products
    selection_id: Mapped[str | None] = mapped_column(ForeignKey("selections.id"))  # the run item that suggested it
    substitution_allowed: Mapped[bool | None] = mapped_column(Boolean)
    substituted_with: Mapped[str | None] = mapped_column(String(256))  # what the shopper substituted, if anything

    order: Mapped[PastOrder] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship()
    selection: Mapped[Selection | None] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


# Top deals lifecycle
DEAL_ACTIVE, DEAL_ADDED, DEAL_DISMISSED, DEAL_RETIRED = "active", "added_to_cart", "dismissed", "retired"


class TopDeal(Base):
    """A deal worth the user's attention, flagged by the agent (or the user) for the Deals page.
    Independent of runs: the user can add it straight to the cart or dismiss it with a reason."""

    __tablename__ = "top_deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price_observation_id: Mapped[int | None] = mapped_column(ForeignKey("price_observations.id"))  # price when flagged
    headline: Mapped[str] = mapped_column(String(128))  # "Lowest we've seen", "29% off"
    why: Mapped[str | None] = mapped_column(Text)  # the agent's reasoning, with numbers
    relevant_to: Mapped[str | None] = mapped_column(String(128), index=True)  # pantry / list item it matters for
    rating: Mapped[int] = mapped_column(Integer, default=2)  # 1 good · 2 great · 3 stock up
    suggested_quantity: Mapped[float] = mapped_column(Float, default=1)
    added_by: Mapped[str] = mapped_column(String(16), default="agent")  # agent | user
    status: Mapped[str] = mapped_column(String(16), default=DEAL_ACTIVE, index=True)
    acted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    response_note: Mapped[str | None] = mapped_column(Text)  # why it was dismissed / retired

    product: Mapped[Product] = relationship()
    observation: Mapped[PriceObservation | None] = relationship()


class Category(Base):
    """No Frills' aisle tree, learned from search facets and product breadcrumbs."""

    __tablename__ = "categories"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    parent_code: Mapped[str | None] = mapped_column(String(16), index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 1 = department (Food), 2 = aisle (Meat), 3 = sub-aisle (Beef)
    path: Mapped[str] = mapped_column(String(256))


class DealScan(Base):
    """One sweep of current deals, aisle by aisle."""

    __tablename__ = "deal_scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running | done | failed
    pages: Mapped[int] = mapped_column(Integer, default=0)
    products_seen: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


# =====================================================================================
# People, secrets, chat, audit, usage, inbox
# =====================================================================================

class User(Base):
    """A person using the app. Created on their first visit through Cloudflare Access; the id is derived from
    their email. Other tables store it (in columns named device_id) to say who did what."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Secret(Base):
    """Encrypted credentials (LLM keys, Notion token, the No Frills session). Write-only over the API."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32))
    ciphertext: Mapped[str] = mapped_column(Text)
    last4: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(32))
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(128), default="New chat")
    mode: Mapped[str] = mapped_column(String(16), default="auto")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(32))
    system: Mapped[bool] = mapped_column(Boolean, default=False)  # created by a job (weekly plan), not a person

    messages: Mapped[list[ChatMessage]] = relationship(back_populates="thread", order_by="ChatMessage.id", cascade="all, delete-orphan")


class ChatMessage(Base):
    """One message in provider-neutral form. role: user | assistant | tool. Tool calls are JSON on the
    assistant message; tool results carry the call id. Pending confirmations live on the tool message."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("chat_threads.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str | None] = mapped_column(Text)
    tool_calls: Mapped[list] = mapped_column(JSONList, default=list)  # [{id, name, arguments}]
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    tool_name: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="done")  # done | pending_confirmation | declined
    device_id: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)

    thread: Mapped[ChatThread] = relationship(back_populates="messages")


class AgentAudit(Base):
    """Every tool the agent ran (or was refused), with its tier and outcome."""

    __tablename__ = "agent_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    thread_id: Mapped[str | None] = mapped_column(String(16), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    arguments: Mapped[str | None] = mapped_column(Text)  # JSON, secrets redacted
    outcome: Mapped[str] = mapped_column(String(16))  # ok | error | pending | declined | refused
    detail: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    device_id: Mapped[str | None] = mapped_column(String(32))


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    thread_id: Mapped[str | None] = mapped_column(String(16))


class InboxEvent(Base):
    """Things that need a person: a plan or run ready, the No Frills session needed, a meal check-in."""

    __tablename__ = "inbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(256))  # app route, e.g. "#/runs/abc"
    dedupe_key: Mapped[str | None] = mapped_column(String(128), index=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# =====================================================================================
# Prompts edited in the app (defaults are the files in shopping/prompts/)
# =====================================================================================

class PromptOverride(Base):
    __tablename__ = "prompt_overrides"

    prompt_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g. "tasks/shop"
    body: Mapped[str] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(String(8))  # fast / smart when changed from the default
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64))


class PromptRevision(Base):
    """Earlier versions, so an edit can be undone (the last 20 per prompt)."""

    __tablename__ = "prompt_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prompt_id: Mapped[str] = mapped_column(String(64), index=True)
    body: Mapped[str] = mapped_column(Text)
    tier: Mapped[str | None] = mapped_column(String(8))
    saved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    saved_by: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(32))
