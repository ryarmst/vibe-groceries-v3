---
title: Plan my week
description: The one-tap weekly plan (Home and Meals): healthy, mostly big-batch meals around this week's deals, saved to Suggested Meals, plus one shopping run with their ingredients, the grocery list and the regular weekly purchases, for you to review.
tier: smart
steps: 16
tools: meals_context, meal_cost, meal_suggest, meal_update, notion_meals, notion_cooking_search, notion_get_page, notion_pantry, notion_grocery_list, shopping_get_context, shopping_search_many, shopping_get_requirement_history, shopping_get_past_orders, shopping_get_past_order, shopping_get_feedback, shopping_stage_items, shopping_get_top_deals, shopping_add_top_deal, get_reference
placeholders: today, weekday, store_id, person, household, meals_per_week
---
Plan this week's meals and build the whole shopping run in one go, with no questions first. People review
everything afterwards.

What makes a good week
- Healthy: every main has a solid protein (meat, fish, eggs, tofu, beans or lentils) and plenty of vegetables.
- Built on deals: choose the protein and main vegetables from what's on sale this week, and say what each saves.
- Mostly big batches: at least 3 of the meals should make 6–8 servings, so two people get dinner plus lunches or
  a second night from each. Plan about 3–4 batch meals rather than a different dinner every night. Skip a meal if
  the leftovers already cover those days.
- Easy: weeknight cooking with little hands-on time (sheet pan, one pot, Instant Pot, rice cooker). At most one
  meal can be more involved, for the weekend.
- Uses what's at home first, especially anything expiring soon, and respects past feedback and rejected meals.

Steps (call tools that don't depend on each other in the same reply)
1. meals_context, notion_grocery_list, notion_pantry (regular_only), shopping_get_past_orders and shopping_get_context.
   If the latest order isn't imported, import it (shopping_get_past_order) so regular purchases are judged
   correctly.
2. shopping_get_requirement_history for every regular at once, and one meal_cost call (`recipes`) for the meals
   you're considering.
3. One meal_suggest call (`meals`) with every meal: full fields (Week, Cook By, Course, Cuisine, Main Ingredient,
   Tags including Bulk where it applies, Effort, Servings, Shopping Ingredients, Uses Up, Based On) and the full
   recipe in `body`, with a 📌 line that names the deals, the cost per serving and how many days of leftovers it gives.
4. One shopping_search_many call covering every ingredient to buy and every regular to restock.
5. One shopping_stage_items call (summary like "Week of Nov 26", submit: true) with:
   - the meals' ingredients, minus what's at home (reason grocery_list, `requirement` = the ingredient, rationale
     naming the meal);
   - every active grocery-list row (reason grocery_list);
   - the regular weekly purchases: every regular that's `due` or `unknown`, and the weekly basics (milk, bread,
     cereal, eggs and the like) unless they were bought in the last 4 days (reason regular_staple).
   Whole packages, best value per unit, sale items where they fit. Respect Shopping Preferences (Avoid, Size,
   Subs) and Store Preference (Costco items only on a really good deal).
6. Re-stage anything that failed (e.g. out of stock) with the next best in one more call (same run_id).

Report: the meals one line each (`Meal — servings · leftovers for N days · the deal it uses · $/serving`), the run
link (`#/runs/<id>`) and its total, then short lines only if needed: regulars skipped as not due, not found,
For Costco.
