// History: activity, your notes, prices, the assistant's log, LLM usage.
import { get, post } from "../lib/api.js";
import { ago, busy, clear, empty, h, link, money, toast, when } from "../lib/ui.js";

const TABS = { activity: "Activity", feedback: "Notes & feedback", prices: "Prices", audit: "Assistant log", usage: "AI usage" };

export async function render(el, _p, query) {
  const tab = TABS[query.tab] ? query.tab : "activity";
  el.append(h("h1", {}, "History"), h("div", { class: "tabs" }, ...Object.entries(TABS).map(([k, label]) =>
    h("button", { class: k === tab ? "on" : "", onclick: () => { location.hash = `#/history?tab=${k}`; } }, label))));
  const box = h("div");
  el.append(box);
  await ({ activity, feedback, prices, audit, usage })[tab](box, query);
}

async function activity(box, query) {
  const d = await get(`/api/history/activity?days=${query.days || 30}${query.q ? `&q=${encodeURIComponent(query.q)}` : ""}`);
  const q = h("input", { value: query.q || "", placeholder: "Filter…" });
  box.append(h("div", { class: "row" }, q, h("button", { onclick: () => { location.hash = `#/history?tab=activity&q=${encodeURIComponent(q.value)}`; } }, "Filter")));
  if (!d.events.length) { box.append(empty("Nothing in the last 30 days.")); return; }
  box.append(h("table", { class: "table" }, h("tbody", {}, ...d.events.map((e) => h("tr", {},
    h("td", { class: "small muted" }, when(e.time)), h("td", {}, h("span", { class: "badge" }, d.kinds[e.kind] || e.kind)),
    h("td", {}, h("strong", {}, e.title), " ", e.product_id ? link(`#/product/${e.product_id}`, e.product || "") : e.product || "",
      e.detail ? h("div", { class: "small muted" }, e.detail) : null, e.note ? h("div", { class: "small" }, `“${e.note}”`) : null),
    h("td", {}, e.run_id ? link(`#/runs/${e.run_id}`, "run") : null))))));
}

async function feedback(box) {
  const d = await get("/api/history/feedback");
  const note = h("input", { placeholder: "A general note for the assistant, e.g. “buy bigger eggs in baking season”", style: { flex: 1 } });
  box.append(h("div", { class: "row" }, note, h("button", { onclick: (e) => busy(e.target, async () => {
    if (!note.value.trim()) return; await post("/api/history/notes", { note: note.value }); toast("Saved."); location.reload();
  }) }, "Save note")));
  box.append(h("p", { class: "small muted" }, `${d.summary.total} notes · ${d.summary.unreviewed} not yet read by the assistant.`));
  if (!d.records.length) { box.append(empty("No notes yet.")); return; }
  box.append(h("table", { class: "table" }, h("tbody", {}, ...d.records.map((r) => h("tr", {},
    h("td", { class: "small muted" }, when(r.date)), h("td", {}, r.requirement || "", h("div", { class: "small muted" }, r.product || "")),
    h("td", { class: "small" }, r.action.replaceAll("_", " "), r.chosen_product ? ` → ${r.chosen_product}` : ""),
    h("td", { class: "small" }, r.note ? `“${r.note}”` : ""),
    h("td", { class: "small muted" }, r.reviewed ? `✓ ${r.resolution || ""}` : ""))))));
}

async function prices(box, query) {
  const d = await get(`/api/history/prices${query.q ? `?q=${encodeURIComponent(query.q)}` : ""}`);
  const q = h("input", { value: query.q || "", placeholder: "Search products…" });
  box.append(h("div", { class: "row" }, q, h("button", { onclick: () => { location.hash = `#/history?tab=prices&q=${encodeURIComponent(q.value)}`; } }, "Search")));
  if (d.recent_changes.length) {
    box.append(h("h2", {}, "Recent price changes"), h("table", { class: "table" }, h("tbody", {}, ...d.recent_changes.map((c) => h("tr", {},
      h("td", {}, link(`#/product/${c.product_id}`, `${c.brand ? c.brand + " " : ""}${c.name}`), c.size_changed ? h("div", { class: "small warn" }, `size ${c.from_size} → ${c.to_size}`) : null),
      h("td", { class: "num" }, `${money(c.from_price)} → ${money(c.to_price)}`, c.change_pct ? h("div", { class: `small ${c.change_pct > 0 ? "bad" : "sale"}` }, `${c.change_pct > 0 ? "+" : ""}${c.change_pct}%`) : null))))));
  }
  box.append(h("h2", {}, "Products"), h("table", { class: "table" }, h("tbody", {}, ...d.products.map((p) => h("tr", {},
    h("td", {}, link(`#/product/${p.product_id}`, `${p.brand ? p.brand + " " : ""}${p.name}`), h("div", { class: "small muted" }, p.aisle || "")),
    h("td", { class: "num" }, money(p.current_price)), h("td", { class: "small muted" }, `${p.observations} prices · ${ago(p.last_seen)}`))))));
}

async function audit(box) {
  const d = await get("/api/audit");
  box.append(h("p", { class: "small muted" }, "Everything the assistant did, including what it asked you to confirm."));
  if (!d.entries.length) { box.append(empty("Nothing yet.")); return; }
  box.append(h("table", { class: "table" }, h("tbody", {}, ...d.entries.map((a) => h("tr", {},
    h("td", { class: "small muted" }, when(a.at)), h("td", {}, a.tool, h("div", { class: "small muted" }, a.tier)),
    h("td", {}, h("span", { class: `badge ${a.outcome === "ok" ? "ok" : a.outcome === "error" ? "bad" : "warn"}` }, a.outcome)),
    h("td", { class: "small" }, h("code", {}, (a.arguments || "").slice(0, 160)), a.detail ? h("div", { class: "muted" }, a.detail.slice(0, 200)) : null),
    h("td", {}, a.thread_id ? link(`#/chat/${a.thread_id}`, "chat") : null))))));
}

async function usage(box) {
  const d = await get("/api/llm/usage");
  const s = d.summary;
  box.append(h("p", {}, `This month: ${money(s.month_spent_usd)} of ${money(s.monthly_cap_usd)} · ${s.month_tokens_in.toLocaleString()} tokens in, ${s.month_tokens_out.toLocaleString()} out · ${s.requests_today} requests today. `,
    link("#/settings", "Change the cap")));
  box.append(h("table", { class: "table" }, h("tbody", {}, ...d.daily.map((r) => h("tr", {},
    h("td", {}, r.day), h("td", {}, `${r.provider} · ${r.model}`), h("td", { class: "num" }, r.requests), h("td", { class: "num" }, `${r.tokens_in.toLocaleString()} / ${r.tokens_out.toLocaleString()}`),
    h("td", { class: "num" }, money(r.cost_usd)))))));
}
