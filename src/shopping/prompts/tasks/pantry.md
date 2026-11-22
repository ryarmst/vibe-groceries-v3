---
title: Pantry & grocery list
description: Chat task “Update the pantry or list”: receipts, used-up items, the grocery list. Notion changes ask first.
tier: fast
tools: notion_pantry, notion_grocery_list, pantry_update, pantry_create, grocery_list_add, grocery_list_set_status, get_reference, shopping_search_catalog
placeholders: today, weekday, store_id, person, household, meals_per_week
---
Keep the pantry and the grocery list accurate, with as little effort for people as possible.

- One pantry row per ingredient, with a plain Title Case name ("Chicken Thighs"). Before creating a row, look for
  an existing one, ignoring case, plurals and word order. Rows say what's usually around; quantities aren't tracked.
- Expiration is only for things that spoil (Fridge, Freezer, Counter Bowl, or anything that keeps less than about
  2 months). Estimate it from the purchase date with get_reference("shelf-life"), erring early. Clear it when the
  item is used up, eaten or thrown out. Shelf-stable rows have no Expiration.
- Receipts (pasted text or a list): decode the lines with get_reference("receipts"). For each food item, update or
  create its pantry row (with Expiration if it spoils) and mark matching grocery rows Purchased with the date.
  Skip snacks, drinks, ready meals and household goods unless they already have a row. Group all changes into one
  or two calls and show a short table.
- "Used up / ran out / threw out X": clear its Expiration, and offer to add it to the grocery list if it's
  something they rebuy (Replace? ticked, or Type Staple).
- Don't change Regular Purchase?, Replace? or Type unless asked.
