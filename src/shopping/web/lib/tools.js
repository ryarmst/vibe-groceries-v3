// What each assistant tool is doing, in plain words, for the progress lines in Chat and Quick Shop.
const LABELS = {
  shopping_get_context: "Checking this week's deals", shopping_search_many: "Searching the store",
  shopping_search_catalog: "Searching past prices", shopping_list_aisles: "Listing aisles",
  shopping_get_product: "Checking a product", shopping_get_product_history: "Checking price history",
  shopping_get_recent_price_history: "Checking recent price changes", shopping_get_cart: "Checking the cart",
  shopping_auth_status: "Checking the No Frills sign-in", shopping_get_run: "Reading a shopping run",
  shopping_list_runs: "Listing shopping runs", shopping_stage_items: "Adding items for you to review",
  shopping_update_selection: "Updating an item", shopping_apply_approved: "Adding approved items to the cart",
  shopping_quick_add: "Adding to the cart", shopping_get_feedback: "Reading your notes",
  shopping_mark_feedback_reviewed: "Marking notes as read", shopping_get_requirement_history: "Checking what's due",
  shopping_get_past_orders: "Checking past orders", shopping_get_past_order: "Reading a past order",
  shopping_get_top_deals: "Checking top deals", shopping_add_top_deal: "Adding a top deal",
  shopping_retire_top_deal: "Removing a top deal",
  notion_pantry: "Reading the pantry", notion_grocery_list: "Reading the grocery list", notion_meals: "Reading suggested meals",
  notion_cooking_search: "Searching recipes", notion_get_page: "Reading a Notion page", meals_context: "Gathering deals, pantry and recipes",
  meal_cost: "Pricing recipes", meal_suggest: "Saving suggested meals", meal_update: "Updating a meal", meal_promote: "Saving a meal to Cooking",
  cooking_save_recipe: "Saving a recipe", pantry_update: "Updating the pantry", pantry_create: "Adding to the pantry",
  grocery_list_add: "Adding to the grocery list", grocery_list_set_status: "Updating the grocery list", get_reference: "Looking something up",
};

export const toolLabel = (name) => LABELS[name] || "Working";
