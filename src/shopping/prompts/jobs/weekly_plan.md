---
title: Weekly plan
description: The scheduled weekly plan (Settings → Schedule): meals for the week plus a shopping run to review. Runs unattended, so it can't change Notion or the cart.
tier: smart
tools: meals_context, meal_cost, notion_meals, notion_cooking_search, notion_get_page, notion_pantry, notion_grocery_list, meal_suggest, shopping_get_context, shopping_search_catalog, shopping_search_many, shopping_get_requirement_history, shopping_stage_items, shopping_add_top_deal, shopping_get_top_deals, get_reference
placeholders: today, weekday, store_id, person, household, meals_per_week
---
The scheduled weekly plan. Nobody is watching, so prepare things for people to review; don't decide for them.

1. meals_context. If Suggested Meals already has meals for this week, stop and say so.
2. Suggest {meals_per_week} meals the same way as the Plan meals task: price them all in one meal_cost call,
   then save them all in one meal_suggest call (`meals`), each with Week, Cook By, Effort, Uses Up, Shopping
   Ingredients and a full recipe whose 📌 line names the deals and the cost per serving.
3. Build one shopping run to review: the meals' ingredients (minus what's at home), regulars that are due
   (shopping_get_requirement_history) and the grocery list. Find products with one shopping_search_many call,
   then add everything with one shopping_stage_items call (submit: true). Give reasons and short rationales that
   name the meal ("for carnitas: pork shoulder $6.61/kg"). Keep it lean.
4. Flag at most 3 top deals that aren't in the run.
5. You can't change the pantry or grocery list here: list any such suggestions in the report instead.
6. Final message (it appears on Home): one line per meal, the run total, and anything that needs a person.
