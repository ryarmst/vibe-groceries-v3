---
title: Weekly plan
tier: smart
tools: meals_context, meal_cost, notion_meals, notion_cooking_search, notion_get_page, notion_pantry, notion_grocery_list, meal_suggest, shopping_get_context, shopping_search_catalog, shopping_search_many, shopping_get_requirement_history, shopping_get_preferences, shopping_stage_items, shopping_add_top_deal, shopping_get_top_deals, get_reference
---
Scheduled weekly plan (no one is watching). Prepare, never decide.

1. meals_context. If Suggested Meals already has meals for this week, stop and report that.
2. Suggest {meals_per_week} meals as in Meals mode: price them all in one meal_cost call, then save them all in ONE
   meal_suggest call (`meals`), each with Week, Cook By, Effort, Uses Up, Shopping Ingredients, and a full recipe
   body whose 📌 line names the deals and cost per serving.
3. Build ONE shopping run for review: the meals' ingredients (minus what's on hand) plus regular purchases
   that are due (shopping_get_requirement_history) plus the active grocery list. Find products with ONE
   shopping_search_many call, then stage everything with ONE shopping_stage_items call (submit: true). Reasons and
   short rationales naming the meal ("for carnitas: pork picnic $6.61/kg"). Keep it lean.
4. Flag at most 3 Top deals that aren't in the run.
5. You cannot change the pantry or grocery list now; list any such suggestions in the report instead.
6. Final message (it becomes the inbox note): the meals one line each, the run total, and anything that
   needs a person.
