# Setup guide

This gets your own copy of vibe-groceries running at an address like `https://groceries.example.com`, for
you and the people you live with. Allow about an hour.

## What you need

- **A computer that stays on** and runs Docker: a home server, NAS, mini PC, Raspberry Pi 4/5 (64-bit), or a
  small cloud server. 1 GB of free memory is plenty. No ports need opening on your router.
- **A domain on Cloudflare** (the free plan is fine). The app lives on a subdomain of it.
- **A No Frills account** (PC id), the one you shop with.
- **A Google Gemini API key** for the assistant: free at [aistudio.google.com](https://aistudio.google.com)
  → Get API key. Other providers (OpenAI, Claude, Groq, OpenRouter, Mistral, a local Ollama) work too.
- Optional: **Notion**, if you want pantry, grocery list, recipes and meal plans (see [docs/notion.md](docs/notion.md)).

**Please read first.** The app talks to No Frills the way the PC Express phone app does. That isn't an
official API, so it can break without notice, and using it is at your own risk with respect to the
retailer's terms. It can fill your cart but can never check out, pay or book a pickup time: you do that on
nofrills.ca. Cloudflare terminates HTTPS, so it can see the traffic.

## 1. Collect three values

1. **PCID_CLIENT_SECRET** and **PCX_API_KEY**: fixed values that every copy of the PC Express app or website
   uses. This project doesn't publish them; they're documented in
   [FireBall1725/pcexpress-mcp-server](https://github.com/FireBall1725/pcexpress-mcp-server)
   (the client secret in `pcid_config.py`, the `x-apikey` in `AUTH_NOTES.md`).
2. **STORE_ID**: your store's number, as used by nofrills.ca (the store locator shows it in the store's page
   address). Not sure? Leave it empty for now: after step 6, choose your store on nofrills.ca and run
   `docker compose exec app vibe pcid check`, which prints your cart's store and its number. Put it in `.env`
   and run `docker compose up -d`.

## 2. Get the code onto the server

```bash
git clone https://github.com/ryarmst/vibe-groceries-v3.git vibe-groceries
cd vibe-groceries/deploy
cp .env.example .env
chmod 600 .env
openssl rand -base64 32        # paste the output as MASTER_KEY in .env
```

Fill in `PUBLIC_URL`, `MASTER_KEY`, the three values from step 1, `HOUSEHOLD_REGION` and `TZ` now.
The Cloudflare values come next.

## 3. Cloudflare Tunnel

The tunnel connects your server to Cloudflare from the inside, so nothing is exposed on your network.

1. Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels** → **Create a tunnel** → **Cloudflared**.
   Name it `vibe-groceries` → **Save tunnel**.
2. Under "Install and run a connector", choose **Docker** and copy only the **token** (the long string after
   `--token`) into `.env` as `TUNNEL_TOKEN`. Don't run the command shown; Docker Compose runs the connector.
3. **Next** → add a published application route (public hostname): your subdomain (e.g. `groceries`) and
   domain, no path; service type **HTTP**, URL **`app:8000`** → **Save**.

## 4. Cloudflare Access (who can open the app)

Access asks for an emailed one-time code before anyone reaches the app. The app checks Access's signed
token too, and only lets in the emails you list in `ALLOWED_USERS`.

1. **One-time PIN**: Zero Trust → **Integrations** → **Identity providers** → **Add new identity provider**
   → **One-time PIN**. (Google sign-in can be added the same way.)
2. Zero Trust → **Access controls** → **Applications** → **Create new application** → **Self-hosted and private**.
3. Name `vibe-groceries`; public hostname: the same subdomain and domain as the tunnel, no path. Session
   duration **1 month** (how often you re-enter a code).
4. Policy: name `Household`, action **Allow**, Include → **Emails** → each person's email. Policy session
   duration **Same as application**. Login method: One-time PIN. **Create**.
5. The application's **Edit** → **Advanced settings** → **Cookie settings**: SameSite **Lax**, HTTP Only **on**,
   binding cookie **on**.
6. Copy into `.env`:
   - `ACCESS_AUD`: the application's **Edit** → **Additional settings** → **Application Audience (AUD) Tag**;
   - `ACCESS_TEAM_DOMAIN`: Zero Trust → **Settings** → **Team name and domain** (`yourteam.cloudflareaccess.com`);
   - `ALLOWED_USERS`: the same emails with first names, e.g. `alex@example.com=Alex,sam@example.com=Sam`.

Use exactly one Access application for this hostname. If you switch on **Protect with Access** for the tunnel
route, pick this application there rather than creating another (the app accepts one AUD tag only).

## 5. Start it

```bash
docker compose up -d --build
docker compose logs -f app      # wait for "vibe-groceries ready: … access=yourteam.cloudflareaccess.com"
```

In Zero Trust → Networks → Tunnels the tunnel should show **Healthy**.

## 6. First visit

Open your address, enter your email and the code you receive. Then, in the app:

1. **Settings → No Frills account → Sign in to No Frills.** A PC id tab opens; sign in with your No Frills
   account. That tab then stays on a blank "loading" page (it's trying to open the phone app): that's
   expected. Copy that tab's full address, paste it into the box in Settings, and choose **Finish signing in**.
   The server keeps the session alive from then on; the app never sees your password.
2. **Settings → API keys**: paste the Gemini key. Keys are stored encrypted on your server and never shown again.
3. **Settings → LLM**: the defaults use Gemini Flash-Lite for quick jobs and Gemini Flash for planning. Press
   **Test** on each. Free-tier limits differ per key: check AI Studio → Rate limits and enter them under
   "Free-tier limits" so the app paces itself.
4. Optional: Notion ([docs/notion.md](docs/notion.md)) and the daily schedule (Settings → Schedule).
5. Try **Quick Shop** on Home: "2 L of 2% milk and bananas". Then check the **Cart** page.

The deal scan runs each morning (about 3 minutes); **Deals → Assess deals** runs it now.

## Day to day

| Task | How |
|---|---|
| Update | `git pull && docker compose up -d --build` (in `deploy/`) |
| Logs | `docker compose logs -f app` |
| No Frills session | `docker compose exec app vibe pcid status` (or Settings) |
| Add or remove a person | Change the Access policy **and** `ALLOWED_USERS`, then `docker compose up -d` |
| Backups | Nightly SQLite copies in the `vibe-groceries_app-data` volume (`/data/backups`, 7 kept). Copy them off the server now and then, and keep `.env` somewhere safe |

If No Frills ever ends the session (a password change, for example), Home shows "No Frills sign-in ended":
sign in again under Settings.
