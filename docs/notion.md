# Notion (optional)

Without Notion the app still does deals, Quick Shop, shopping runs from chat, the cart, orders and price
history. With it, the assistant also knows your pantry and grocery list, plans meals and saves recipes.

## Connect

1. Notion → **Settings** → **Connections** → **Develop or manage integrations** → **New integration**
   (internal). Copy its token into the app: **Settings → API keys → Notion**.
2. On each database below: **⋯** → **Connections** → add your integration.
3. Copy each database's data source id (**⋯** → **Manage data sources** → **⋯** → **Copy data source ID**, or
   the database link) into **Settings → Notion**. Any you leave empty simply turn that feature off.

## Databases

Names must match exactly; extra properties of your own are fine, and a missing one is left empty.

**Kitchen Pantry and Storage** (one row per ingredient you keep)

| Property | Type | Notes |
|---|---|---|
| Name | Title | |
| Type | Select | e.g. Produce, Dairy, Meat, Pantry |
| Location, Sub-Location | Select | Fridge, Freezer, Pantry… |
| Expiration | Date | Perishables only; drives "use it up" suggestions |
| Regular Purchase? | Checkbox | Staples the shop checks for |
| Replace? | Checkbox | |
| Store Preference | Select | e.g. No Frills, Costco (Costco items are only bought at No Frills on a great deal) |
| Shopping Preferences | Text | Lines like `Prefer: …`, `Avoid: …`, `Size: …`, `Buy when: under $4`, `Subs: never` |

**Groceries** (the shopping list)

| Property | Type |
|---|---|
| Name | Title |
| Quantity | Text |
| Status | Status or Select with **New** (to buy) and **Purchased** |
| Purchase Date | Date |
| a relation to Kitchen Pantry and Storage | Relation (any name containing "Pantry") |

**Cooking** (your recipes): Name (Title), Course (Select: Main, Soup, Salad, Breakfast, Side…), Cuisine,
Main Ingredient, Tags (Multi-select), Effort (Select: Easy, Moderate, Gourmet), Shopping Ingredients (Text).
The recipe itself is the page body.

**Suggested Meals** (where the planner writes): import [`notion/suggested-meals-import.csv`](notion/suggested-meals-import.csv)
(**⋯** → **Import** → **CSV**), rename it **Suggested Meals**, then change these column types:

| Column | Type | Then |
|---|---|---|
| Status | Status | To-do = Suggested · In progress = Planned · Complete = Cooked, Promoted, Rejected, Skipped |
| Week, Cook By | Date | |
| Course, Effort, Source | Select | |
| Tags | Multi-select | |
| Servings | Number | |
| Grocery Run | URL | |
| Promote | Checkbox | |

Add three relations by hand: **Uses Up** → Kitchen Pantry and Storage, **Based On** → Cooking,
**Promoted To** → Cooking. Then delete the `EXAMPLE … (delete me)` rows (the options stay).
