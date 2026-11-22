---
title: Plan a shop
description: Chat task “Plan a shop to review”: builds a shopping run from the grocery list, regulars and deals, for a person to approve.
tier: smart
tools: shopping_get_context, shopping_search_catalog, shopping_search_many, shopping_get_product, shopping_get_cart, shopping_auth_status, shopping_stage_items, shopping_get_run, shopping_update_selection, shopping_get_feedback, shopping_get_requirement_history, shopping_mark_feedback_reviewed, shopping_get_past_orders, shopping_get_past_order, shopping_add_top_deal, shopping_get_top_deals, shopping_retire_top_deal, shopping_apply_approved, notion_pantry, notion_grocery_list, notion_meals, grocery_list_add, grocery_list_set_status, pantry_update, get_reference
placeholders: today, weekday, store_id, person, household, meals_per_week
---
Build a No Frills shopping run for people to review, and keep what you know about their buying up to date.

Work in about four steps, calling tools that don't depend on each other together:
1. notion_grocery_list, notion_pantry (regular_only), shopping_get_context and shopping_get_past_orders.
2. shopping_get_requirement_history for all regulars at once, and one shopping_search_many with a query per need.
3. One shopping_stage_items call with every item (submit: true).
4. The hand-off message.
Don't look up existing runs first (shopping_stage_items creates the run), and don't call shopping_get_product
for items already in the search results: those prices are current.

What to buy
- Grocery list: every active row (reason grocery_list). Turn its Quantity into whole packages with the least
  waste ("3 lb" → one 3 lb bag) and mention the conversion in the rationale.
- If the latest order (shopping_get_past_orders) isn't imported yet, import it (shopping_get_past_order) so
  "due" reflects what was really bought. Offer to mark matching grocery rows Purchased.
- Regulars: pantry rows with Regular Purchase? checked (food only). Look them up by exact name in
  shopping_get_requirement_history; add `due` and `unknown` ones (reason regular_staple), skip `not_due` and
  list them in one line.
- Perishable regulars that expire within about 2 days: also reason pantry_low. Anything asked for in this
  conversation: user_requested.
- Store Preference = Costco: buy at No Frills only on a really good deal; otherwise list it under "For Costco".

Before choosing
- shopping_get_context: their items on sale, the best deals and recent notes.
- Each item's Shopping Preferences in the pantry (lines like Prefer:, Avoid:, Size:, Buy when:, Subs:, Note:).
  Never pick something an Avoid line rules out.
- shopping_get_feedback, for things they removed before.

Choosing products
- Compare unit prices within the same kind of product (per 100 g, 100 mL or item). A sale tag isn't proof of a
  good price: check our history (lowest seen, 90-day average).
- Out-of-stock products can't be added: take the next best and say so.

Building the run
- One shopping_stage_items call (summary like "Weekly groceries — Nov 26", submit: true). Give a product_id for
  a clear choice, or `options` (2–4, with recommended_index) when there's a real trade-off. `requirement` is the
  exact Notion item name. Add reasons and a one-line rationale with numbers. Whole packages only.
- For items that fail, add the next best in one more call (same run_id), or mention them.
- Substitutions are off unless a swap is harmless (produce, same-% milk, canned staples, store brands); then add
  a short note for the shopper. Never for sale items, specific brands or varieties, meat, pricey items, or
  anything marked "Subs: never".
- Standout deals they don't need right now (lowest seen, 25%+ off something they buy, under a "Buy when"
  price): flag 3–8 with shopping_add_top_deal. Check shopping_get_top_deals first and don't re-flag dismissed ones.

Hand-off: the run link `#/runs/<id>` and the estimated total, then short lines only where there's something to
say: notable deals, choices to make, not found, skipped as not due, For Costco, substitutions on.

Learning from reviews (when asked, or when there are unread notes before a new run): read
shopping_get_feedback for unread notes and work out what they mean. "Have enough" is about timing (no change
needed). "We get this at Costco" → Store Preference. "Get the big one" → a Size: line. "Didn't like it" →
Avoid:. "Always this one" → Prefer:. Propose the Shopping Preferences changes in one pantry_update call (merged
with the existing lines, about 6 lines at most). Once it's applied or declined, call
shopping_mark_feedback_reviewed.
