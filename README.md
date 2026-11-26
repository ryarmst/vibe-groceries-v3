# vibe-groceries

A self-hosted grocery and meal assistant for a household that shops at No Frills. It's a web app you run
on your own server:

- **Quick Shop**: type "2 L milk, eggs and stuff for tacos" and good-value picks go straight into your cart.
- **Shopping runs**: the assistant builds a run from your grocery list, regulars that are due and this week's
  deals; you approve or remove items, and approved items go into the cart.
- **Deals**: a daily scan of every aisle's sales, ranked against what you actually buy (lowest price seen,
  stock-up alerts), plus the assistant's picks of great deals on things you haven't tried.
- **Meals** (with Notion): suggestions built around the deals and what's about to expire, with recipes.
- **Chat**: ask about prices, past orders, the pantry; paste a receipt; save recipes.
- **Prompts you can edit**: every instruction the assistant gets is plain text on the **Prompts** page
  (describe your household, change house rules, tune a task), with history and one-click reset.

It **can't check out, pay or book a pickup time**: you do that on nofrills.ca.

**→ [Setup guide](SETUP.md)**

## How it fits together

- One Docker container (FastAPI + SQLite) plus a Cloudflare Tunnel; Cloudflare Access decides who can open it.
- The assistant works with Fireworks AI, Gemini, OpenAI, Claude, Groq, OpenRouter, Mistral or a local model. Each task (Quick Shop, plan a shop, meals, pantry, recipes, ask) has its own short prompt and tool
  list. All prompts are Markdown files in [`src/shopping/prompts/`](src/shopping/prompts/README.md), editable
  in the app.
- No Frills sign-in happens once in your browser; the server keeps the session alive. See [docs/auth.md](docs/auth.md).
- Optional Notion integration for the pantry, grocery list, recipes and suggested meals: [docs/notion.md](docs/notion.md).

## Safety rails

- The retailer client allows only search, product, cart read/update and order-history calls. There's no
  code for checkout, payment, timeslots or account changes.
- The assistant adds to the cart only in Quick Shop, when a person asked; everywhere else it stages runs for
  approval. Scheduled jobs can't add to the cart or change Notion, and Notion edits from chat ask first.
- API keys and the No Frills session are encrypted with your MASTER_KEY and never sent to the browser.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -c constraints.txt -e '.[dev]'
cp .env.example .env      # MASTER_KEY from: .venv/bin/vibe master-key
.venv/bin/vibe-groceries  # http://127.0.0.1:8765, signed in as DEV_USER (loopback only)
.venv/bin/pytest -q
```

Layout: `src/shopping/` (server), `src/shopping/web/` (the web app: plain JS modules, no build step),
`deploy/` (Docker Compose), `tests/`.

## Credits

The PC id sign-in approach comes from [FireBall1725/pcexpress-mcp-server](https://github.com/FireBall1725/pcexpress-mcp-server).
Not affiliated with Loblaw Companies or No Frills.
