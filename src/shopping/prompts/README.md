# Prompts

Everything the assistant is told lives here, as Markdown with a small header. These files are the
defaults: you can edit any of them in the app (**Prompts** page), which saves your version in the database
with a history, and **Reset** brings back the file's text. Editing the files themselves also works (rebuild
the container afterwards); an edit made in the app wins over the file.

| File | Used for |
|---|---|
| `about.md` | Your household: tastes, diets, equipment. Empty by default; added to everything below |
| `household.md` | Shared rules at the top of every chat task and the weekly plan |
| `tasks/*.md` | One per chat task (Quick Shop, Plan a shop, Plan meals, Pantry, Recipes, Ask) |
| `jobs/weekly_plan.md` | The scheduled weekly plan |
| `jobs/deal_review.md` | The daily deal picks (must keep the JSON reply format) |
| `references/*.md` | Notes the assistant looks up when needed (receipts, shelf life) |

Header fields: `title`, `description`, `tier` (fast or smart model), `tools`, `placeholders`.
Placeholders like `{household}` or `{today}` are filled in when the prompt is used.

`tools` can only be changed in these files, not in the app, because a task's tools decide what it can
change (for example, only Quick Shop can add to the cart). The Prompts page shows them but can't edit them.
