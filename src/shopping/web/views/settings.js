// Settings: each section folds to one line with its status, and opens when you need it.
// No Frills (PC id) sign-in, API keys (write-only), AI models and budget, Notion, schedule.
import { changed, del, get, post, put } from "../lib/api.js";
import { ago, busy, clear, h, money, more, toast, when } from "../lib/ui.js";

const PROVIDER_LABELS = { gemini: "Google Gemini", openai: "OpenAI", anthropic: "Anthropic (Claude)", groq: "Groq",
  openrouter: "OpenRouter", mistral: "Mistral", custom: "Custom (OpenAI-compatible)" };
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export async function render(el) {
  el.append(h("h1", {}, "Settings"));
  const [all, secrets, usage, me] = await Promise.all([get("/api/settings"), get("/api/settings/secrets"),
    get("/api/llm/usage"), get("/api/me")]);
  const keysSet = secrets.secrets.filter((x) => x.set).length;
  const aiKey = secrets.secrets.some((x) => x.set && x.name.startsWith("llm:"));
  const notionOn = secrets.secrets.some((x) => x.set && x.name === "notion") && Object.values(all.notion).some(Boolean);
  const j = all.jobs;
  el.append(
    await nofrillsCard(),
    note(keysCard(secrets.secrets), aiKey ? `${keysSet} saved` : "Add an AI key to use the assistant", !aiKey),
    note(llmCard(all.llm, all.providers), `${all.llm.tiers.fast.model} · ${all.llm.tiers.smart.model}`),
    note(budgetCard(all.budget, usage.summary, usage.models_today || []), `${money(usage.summary.month_spent_usd)} of ${money(usage.summary.monthly_cap_usd)} this month`),
    note(notionCard(all.notion), notionOn ? "Connected" : "Not set up (optional)"),
    note(jobsCard(j), [`deals at ${j.deals_hour}:00`, j.weekly_plan_enabled ? "weekly plan on" : null].filter(Boolean).join(" · ")),
    h("div", { class: "card" }, h("p", { style: { margin: 0 } }, "The assistant's instructions are on the ", h("a", { href: "#/prompts" }, "Prompts"),
      " page: describe your household, change house rules, or tune a task.")),
    h("p", { class: "small muted" }, "Signed in as ", h("strong", {}, me.name), ` (${me.email}) through Cloudflare Access.`));
}

// A section that folds to its title and a one-line status.
function section(title, ...body) {
  return h("details", { class: "card fold" }, h("summary", {}, h("h2", {}, title), h("span", { class: "small muted fold-note" })), ...body);
}

function note(card, text, open = false) {
  card.querySelector(".fold-note").textContent = text;
  if (open) card.open = true;
  return card;
}

function field(label, input, hint) {
  return h("label", {}, label, input, hint ? h("span", { class: "small muted" }, hint) : null);
}

// The household's No Frills account. PC ID's sign-in page only works in a real browser, so: open it in a
// new tab, sign in there, then paste back the address that tab ends on (it carries a one-time code).
async function nofrillsCard() {
  const s = await get("/api/retailer/session");
  const card = section("No Frills account");
  const body = h("div");
  card.append(body);
  const draw = (st) => {
    clear(body);
    if (st.state === "connected") {
      body.append(
        h("p", {}, "✓ Signed in", st.signed_in_by ? ` by ${st.signed_in_by}` : "", st.signed_in_at ? ` on ${when(st.signed_in_at)}` : "", ". ",
          h("span", { class: "muted" }, "The session renews itself", st.last_renewed_at ? ` (last ${ago(st.last_renewed_at)})` : "", ".")),
        h("p", { class: "small muted" }, "Shared by everyone using the app. The app never sees your password: the server keeps an encrypted sign-in ",
          "that renews itself. If No Frills ever ends it, sign in again here."),
        h("div", { class: "row" },
          h("button", { onclick: () => startFlow() }, "Sign in again"),
          h("button", { class: "danger", onclick: (e) => busy(e.target, async () => {
            if (!confirm("Sign the app out of No Frills? Cart features stop until someone signs in again.")) return;
            draw(await post("/api/retailer/signout"));
            changed();
          }) }, "Sign out")));
    } else {
      body.append(h("p", {}, "Not signed in. Cart features (adding items, Quick Shop, order import) need the household's No Frills (PC id) account."),
        h("button", { class: "primary", onclick: () => startFlow() }, "Sign in to No Frills"));
    }
  };
  const startFlow = async () => {
    // Open the tab straight from the click (pop-up blockers), then point it at the sign-in URL.
    const tab = window.open("about:blank", "_blank");
    let r;
    try { r = await post("/api/retailer/signin/start"); } catch (e) { tab?.close(); toast(e.message, "err"); return; }
    if (!r.url) { tab?.close(); toast(r.error, "err"); return; }
    if (tab) { tab.opener = null; tab.location.href = r.url; }
    const address = h("input", { placeholder: "https://accounts.pcid.ca/login/success?redirectURL=com.loblaw.pcx…", style: { width: "100%" },
      autocomplete: "off", spellcheck: false });
    const finish = h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
      const res = await post("/api/retailer/signin/finish", { redirect: address.value.trim() });
      if (!res.ok) { toast(res.error, "err"); return; }
      toast("Signed in to No Frills.");
      draw(res);
      changed();
    }) }, "Finish signing in");
    clear(body).append(
      h("ol", {},
        h("li", {}, tab ? "A PC id sign-in tab opened. " : h("span", {}, h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer" }, "Open the PC id sign-in"), ". "),
          "Sign in there with the household's No Frills account."),
        h("li", {}, "That tab then stays on a blank “loading” page (it's trying to open the PC Express phone app). That's expected."),
        h("li", {}, "Copy that tab's full address from the address bar, paste it below, and finish. (Within 30 minutes.)")),
      field("Address of the PC id tab", address), h("div", { class: "row", style: { marginTop: "8px" } }, finish,
        h("button", { class: "link", onclick: async () => draw(await get("/api/retailer/session")) }, "Cancel")));
    address.focus();
  };
  draw(s);
  return note(card, s.state === "connected" ? `Signed in${s.signed_in_by ? ` by ${s.signed_in_by}` : ""}` : "Not signed in", s.state !== "connected");
}

function budgetCard(b, s, models) {
  const cap = h("input", { type: "number", min: 0, max: 1000, step: 1, value: b.monthly_usd, style: { width: "7em" } });
  const warn = h("input", { type: "number", min: 1, max: 100, value: b.warn_at_pct, style: { width: "5em" } });
  const daily = h("input", { type: "number", min: 1, max: 10000, value: b.daily_requests, style: { width: "6em" } });
  const save = (value) => put("/api/settings/budget", value).then(() => { toast("Budget saved."); location.reload(); });
  return section("AI budget",
    h("p", {}, `This month: ${money(s.month_spent_usd)} of ${money(s.monthly_cap_usd)} (${s.pct}%) · ${s.requests_today} requests today. `,
      s.warn ? h("strong", { class: "warn" }, "Near the cap.") : null),
    h("p", { class: "small muted" }, "Gemini's free tier counts as $0. Paid models without a price below are counted at a high estimate, to be safe."),
    models.length ? h("table", { class: "table small" }, h("tbody", {}, ...models.map((m) => h("tr", {},
      h("td", {}, `${m.provider} · ${m.model}`),
      h("td", {}, m.rpd ? `${m.used_today} of ${m.rpd} requests today` : `${m.used_today} requests today`, m.rpm ? ` · ${m.rpm}/min` : ""),
      h("td", {}, m.exhausted ? h("span", { class: "badge bad" }, "out for today") : null))))) : null,
    h("div", { class: "row" }, h("span", { class: "small" }, "Quick set:"),
      ...[5, 10, 25, 50].map((v) => h("button", { onclick: () => save({ monthly_usd: v }) }, `$${v}`))),
    h("div", { class: "form-grid", style: { marginTop: "8px" } }, field("Monthly cap ($)", cap), field("Warn at (%)", warn), field("Requests per day", daily)),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: (e) => busy(e.target, () =>
      save({ monthly_usd: Number(cap.value), warn_at_pct: Number(warn.value), daily_requests: Number(daily.value) })) }, "Save budget")));
}

function llmCard(llm, providers) {
  const tierRow = (tier, help) => {
    const provider = h("select", {}, ...providers.map((p) => h("option", { value: p, selected: llm.tiers[tier].provider === p }, PROVIDER_LABELS[p] || p)));
    const model = h("input", { value: llm.tiers[tier].model, list: `models-${tier}`, style: { width: "16em" } });
    const list = h("datalist", { id: `models-${tier}` });
    return { provider, model, row: h("div", { class: "card" }, h("div", { class: "between" }, h("strong", {}, tier === "fast" ? "Fast model" : "Smart model"),
      h("span", { class: "small muted" }, help)),
      h("div", { class: "form-grid" }, field("Provider", provider), field("Model", h("span", {}, model, list))),
      h("div", { class: "row", style: { marginTop: "6px" } },
        h("button", { onclick: async (e) => {
          const r = await busy(e.target, () => get(`/api/llm/models/${provider.value}`));
          if (!r) return; // already reported
          clear(list).append(...(r.models || []).map((m) => h("option", { value: m })));
          toast(r.error || `${(r.models || []).length} models available; start typing in the Model box.`, r.error ? "err" : "ok");
        } }, "Load models"),
        h("button", { onclick: async (e) => {
          const r = await busy(e.target, () => post("/api/llm/test", { provider: provider.value, model: model.value }));
          if (!r) return;
          toast(r.ok ? `Works: “${r.reply}”` : r.error, r.ok ? "ok" : "err");
        } }, "Test"))) };
  };
  const fast = tierRow("fast", "Quick Shop, questions, the pantry, daily deal picks");
  const smart = tierRow("smart", "Shopping runs and meal plans");
  const fallback = h("textarea", { rows: 2, placeholder: "groq: llama-3.3-70b-versatile" },
    (llm.fallback || []).map((f) => `${f.provider}: ${f.model}`).join("\n"));
  const custom = h("input", { type: "url", value: llm.custom_base_url || "", placeholder: "http://localhost:11434/v1" });
  const temp = h("input", { type: "number", step: 0.1, min: 0, max: 1.5, value: llm.temperature, style: { width: "5em" } });
  const steps = h("input", { type: "number", min: 1, max: 40, value: llm.max_steps, style: { width: "5em" } });
  const limits = h("textarea", { rows: 2, placeholder: "gemini-flash-latest: 5, 20" },
    Object.entries(llm.limits || {}).map(([m, l]) => `${m}: ${l.rpm ?? ""}, ${l.rpd ?? ""}`).join("\n"));
  const prices = h("textarea", { rows: 2, placeholder: "claude-sonnet-4-5: 3, 15" },
    Object.entries(llm.prices || {}).map(([m, [i, o]]) => `${m}: ${i}, ${o}`).join("\n"));
  const parseLines = (text, fn) => text.value.split("\n").map((l) => l.trim()).filter(Boolean).map(fn);
  return section("AI models",
    fast.row, smart.row,
    field("Free-tier limits (model: per minute, per day)", limits, "The app stays under these and switches models when one runs out for the day. Find yours in AI Studio → Rate limits."),
    more("Advanced: backup models, prices, custom endpoint",
      h("div", { class: "form-grid" },
        field("Backup models (provider: model, one per line)", fallback, "Tried in order if both models above fail."),
        field("Custom endpoint base URL", custom, "For the Custom provider, e.g. a local Ollama."),
        field("Prices for paid models (model: input, output, in $ per million tokens)", prices, "Used for the budget."),
        field("Temperature (0 = predictable, higher = more varied)", temp), field("Most steps per reply", steps))),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
      await put("/api/settings/llm", {
        tiers: { fast: { provider: fast.provider.value, model: fast.model.value.trim() }, smart: { provider: smart.provider.value, model: smart.model.value.trim() } },
        fallback: parseLines(fallback, (l) => { const [p, ...m] = l.split(":"); return { provider: p.trim(), model: m.join(":").trim() }; }),
        custom_base_url: custom.value.trim(), temperature: Number(temp.value), max_steps: Number(steps.value),
        prices: Object.fromEntries(parseLines(prices, (l) => { const [m, rest] = l.split(/:(?=[^:]*$)/); return [m.trim(), rest.split(",").map(Number)]; })),
        limits: Object.fromEntries(parseLines(limits, (l) => {
          const [m, rest] = l.split(/:(?=[^:]*$)/); const [rpm, rpd] = rest.split(",").map((x) => parseInt(x, 10));
          return [m.trim(), Object.fromEntries([["rpm", rpm], ["rpd", rpd]].filter(([, v]) => v > 0))];
        })),
      });
      toast("AI settings saved.");
    }) }, "Save AI settings")));
}

function keysCard(secrets) {
  const rows = secrets.map((s) => {
    const input = h("input", { type: "password", autocomplete: "off", placeholder: s.set ? "Replace…" : "Paste key…", style: { width: "18em" } });
    return h("tr", {},
      h("td", {}, s.label, h("div", { class: "small muted" }, s.name)),
      h("td", { class: "small" }, s.set ? `set · …${s.last4 || "••••"} · ${ago(s.updated_at)}` : h("span", { class: "muted" }, "not set"),
        s.last_used_at ? h("div", { class: "muted" }, `used ${ago(s.last_used_at)}`) : null),
      h("td", {}, h("div", { class: "row" }, input,
        h("button", { onclick: (e) => busy(e.target, async () => {
          if (!input.value.trim()) return;
          await put(`/api/settings/secrets/${encodeURIComponent(s.name)}`, { value: input.value.trim() });
          input.value = "";
          toast("Saved. The key is stored encrypted on the server and never shown again.");
          location.reload();
        }) }, "Save"),
        s.set ? h("button", { class: "danger", onclick: (e) => busy(e.target, async () => {
          if (!confirm(`Remove the ${s.label}?`)) return;
          await del(`/api/settings/secrets/${encodeURIComponent(s.name)}`);
          location.reload();
        }) }, "Remove") : null)));
  });
  return section("API keys",
    h("p", { class: "small muted" }, "Keys are stored encrypted on the server. They're never kept in this browser or shown again."),
    h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("tbody", {}, ...rows))));
}

function notionCard(n) {
  const inputs = Object.fromEntries(["pantry", "groceries", "cooking", "meals"].map((k) => [k, h("input", { value: n[k] || "", placeholder: "data source id", style: { width: "100%" } })]));
  return section("Notion",
    h("p", { class: "small muted" }, "Create an internal integration in Notion, share these databases with it, and save its token under API keys. ",
      "Then paste each database's data source ID (or its link) here. Leave a box empty to skip that feature."),
    h("div", { class: "form-grid" }, field("Kitchen Pantry and Storage", inputs.pantry), field("Groceries", inputs.groceries),
      field("Cooking", inputs.cooking), field("Suggested Meals", inputs.meals)),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
      await put("/api/settings/notion", Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.value.trim()])));
      toast("Notion settings saved.");
    }) }, "Save Notion settings")));
}

function jobsCard(j) {
  const enabled = h("input", { type: "checkbox", checked: j.weekly_plan_enabled });
  const dayIn = h("select", {}, ...DAYS.map((d, i) => h("option", { value: i, selected: j.weekly_plan_weekday === i }, d)));
  const hour = h("input", { type: "number", min: 0, max: 23, value: j.weekly_plan_hour, style: { width: "5em" } });
  const meals = h("input", { type: "number", min: 1, max: 14, value: j.meals_per_week, style: { width: "5em" } });
  const checkins = h("input", { type: "checkbox", checked: j.checkins_enabled });
  const checkinHour = h("input", { type: "number", min: 0, max: 23, value: j.checkin_hour, style: { width: "5em" } });
  const orders = h("input", { type: "checkbox", checked: j.order_import_enabled });
  const dealsHour = h("input", { type: "number", min: 0, max: 23, value: j.deals_hour, style: { width: "5em" } });
  const alerts = h("input", { type: "checkbox", checked: j.deal_alerts });
  const review = h("input", { type: "checkbox", checked: j.deal_review });
  return section("Schedule",
    h("label", { class: "row" }, enabled, "Plan the week automatically (meals + a shopping run to review)"),
    h("div", { class: "form-grid" }, field("Day (new flyer: Thursday)", dayIn), field("Hour", hour), field("Meals per week", meals)),
    h("label", { class: "row", style: { marginTop: "10px" } }, checkins, "Daily check-in on planned meals at", checkinHour, ":00"),
    h("label", { class: "row" }, orders, "Import new No Frills orders each evening"),
    h("label", { class: "row", style: { marginTop: "10px" } }, "Check deals every day at", dealsHour, ":00 (no No Frills login needed)"),
    h("label", { class: "row" }, review, "Also ask the assistant to pick great deals on things you haven't bought (one AI request a day)"),
    h("label", { class: "row" }, alerts, "Send a phone alert for stock-up deals (needs an ntfy topic under API keys)",
      h("button", { class: "link", onclick: async (e) => {
        const r = await busy(e.target, () => post("/api/settings/notify/test"));
        if (!r) return;
        toast(r.ok ? "Test alert sent." : r.error, r.ok ? "ok" : "err");
      } }, "Send test alert")),
    h("p", { class: "small muted" }, "To get alerts, install the ntfy app, subscribe to a long random topic name (e.g. vibe-groceries-k3j9x…), and save ",
      "that name under API keys → ntfy topic. Anyone who knows the name can read the alerts, so make it hard to guess."),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
      await put("/api/settings/jobs", { weekly_plan_enabled: enabled.checked, weekly_plan_weekday: Number(dayIn.value),
        weekly_plan_hour: Number(hour.value), meals_per_week: Number(meals.value), checkins_enabled: checkins.checked,
        checkin_hour: Number(checkinHour.value), order_import_enabled: orders.checked,
        deals_hour: Number(dealsHour.value), deal_alerts: alerts.checked, deal_review: review.checked });
      toast("Schedule saved.");
    }) }, "Save schedule")));
}
