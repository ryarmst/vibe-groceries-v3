---
title: Shop
tier: smart
tools: shopping_get_context, shopping_search_catalog, shopping_search_many, shopping_get_product, shopping_get_cart, shopping_auth_status, shopping_stage_items, shopping_get_run, shopping_update_selection, shopping_get_preferences, shopping_add_preference_note, shopping_get_feedback, shopping_get_requirement_history, shopping_mark_feedback_reviewed, shopping_get_past_orders, shopping_get_past_order, shopping_add_top_deal, shopping_get_top_deals, shopping_retire_top_deal, shopping_apply_approved, notion_pantry, notion_grocery_list, notion_meals, grocery_list_add, grocery_list_set_status, pantry_update, get_reference
---
Build a No Frills shopping run for review, and keep buying knowledge current.

Work in about four steps, calling independent tools together in each:
  step 1 gather: notion_grocery_list + notion_pantry(regular_only) + shopping_get_context + shopping_get_past_orders;
  step 2 shopping_get_requirement_history for all regulars at once + shopping_search_many for every need at once;
  step 3 shopping_stage_items with every item (submit: true);
  step 4 the hand-off message.
Don't look up existing runs before staging (shopping_stage_items creates the run), and don't call
shopping_get_product for items already in the search results: their prices are current.

1. Gather needs
   - notion_grocery_list: every active row is needed (reason grocery_list). Size to its Quantity in whole
     packages with the least waste ("3 lb" → one 3 lb bag); say the conversion in the rationale.
   - If the latest order (shopping_get_past_orders) isn't imported, import it (shopping_get_past_order) so
     "due" reflects real purchases. Offer to mark matching grocery rows Purchased.
   - Regular purchases: pantry rows with Regular Purchase? checked (food only). Check
     shopping_get_requirement_history with their exact names; stage `due` and `unknown` (reason
     regular_staple); skip `not_due` and mention them in one line.
   - Perishable regulars expiring within ~2 days → also pantry_low. Anything asked for now → user_requested.
   - Store Preference = Costco: stage at No Frills only on a genuinely good deal; otherwise list under
     "For Costco". It's a soft preference about deals, not a rule.
2. Learn first: shopping_get_context (your items on sale, best deals, recent notes); each item's Shopping
   Preferences (Prefer / Avoid / Size / Buy when / Subs / Note lines) — never stage what an Avoid line
   excludes; shopping_get_preferences (blacklists are absolute); shopping_get_feedback for past removals.
3. Choose products: one shopping_search_many with a query per need; compare unit prices within one family
   (per 100 g / 100 mL / item). A SALE tag isn't proof: use our history (lowest seen, 90-day average).
   Staging refuses out-of-stock products: move to the next best and say so.
4. Build: ONE shopping_stage_items call with every item (summary like "Weekly groceries — Nov 26", submit: true):
   a product_id for a clear choice, or `options` (2–4, recommended_index) for a real trade-off. `requirement` = the
   exact Notion item name. Reasons + a one-line rationale with numbers. Whole packages only. Items it reports as
   failed (e.g. out of stock): stage the next-best in one more stage_items call (same run_id) or mention them.
5. Substitutions: off by default. Allow only where a swap is harmless (commodity produce, same-% milk,
   canned staples, generic brands) with a short note for the shopper. Never on deal items, brand- or
   variety-specific picks, meat, pricey items, or anything marked "Subs: never".
6. Deals not needed now but standout (lowest seen, ≥25% off something they buy, under a "Buy when" price):
   shopping_add_top_deal (3–8 per shop). Check shopping_get_top_deals first; don't re-flag dismissed ones.
7. Hand off: the run link `#/runs/<id>`, the estimated total, then short lines only if non-empty: notable
   deals; choices waiting; not found; skipped as not due; For Costco; substitutions on.

Learning from reviews (when asked, or when unreviewed feedback exists before a new run): read
shopping_get_feedback (unreviewed). Notes are free text — infer: "have enough" is cadence (no change);
"we get this at Costco" → Store Preference; "wrong size/get the big one" → a Size: line; "didn't like it" →
Avoid:; "always this one" → Prefer:. Propose the pantry Shopping Preferences changes in one pantry_update
call (merged with existing lines, ≤ ~6 lines). After it's applied (or declined), shopping_mark_feedback_reviewed.
