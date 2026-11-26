// Home: Quick Shop, then only what needs a person. Deal alerts are grouped so they don't crowd out the rest.
import { get, post } from "../lib/api.js";
import { planMyWeek } from "../lib/chat.js";
import { ago, busy, empty, h, link, more } from "../lib/ui.js";
import { quickBox } from "./quick.js";

const ICONS = { needs_session: "🔌", run_review: "🧾", plan_ready: "🍽️", meal_checkin: "👩‍🍳", meal_skipped: "⏭️",
  order_imported: "📦", job_failed: "⚠️", deal_alert: "🔥" };

export async function render(el) {
  const inbox = await get("/api/inbox");
  const s = inbox.retailer_session;
  const quick = quickBox({ compact: true });
  el.append(h("h1", {}, `Hi ${inbox.user.name.split(" ")[0]}`));
  if (s.state !== "connected") {
    el.append(h("div", { class: "flash err" }, "No Frills isn't signed in, so nothing can go into the cart. ", link("#/settings", "Sign in")));
  }
  el.append(h("section", { class: "card" }, h("h2", { style: { marginTop: 0 } }, "⚡ Quick Shop"), quick),
    h("section", { class: "card between" },
      h("div", { class: "grow" }, h("h2", { style: { margin: 0 } }, "🗓️ Plan my week"),
        h("p", { class: "small muted", style: { margin: "4px 0 0" } }, "Healthy batch meals built on this week's deals, plus one shopping run ",
          "with their ingredients, your list and the weekly regulars. You review it before anything goes in the cart.")),
      h("button", { class: "primary", onclick: (e) => busy(e.target, planMyWeek) }, "Plan my week")));

  const deals = inbox.items.filter((i) => i.kind === "deal_alert");
  const tasks = inbox.items.filter((i) => i.kind !== "deal_alert");
  el.append(h("h2", {}, "Needs you"));
  if (!tasks.length && !deals.length) el.append(empty("Nothing waiting."));
  for (const item of tasks) el.append(itemCard(item));
  if (deals.length) el.append(dealAlerts(deals));

  const bits = [inbox.deal_scan.running ? "checking deals now" : inbox.deal_scan.last ? `deals checked ${ago(inbox.deal_scan.last.finished_at)}` : null,
    inbox.cart.pending ? `adding ${inbox.cart.pending} item(s) to the cart` : null].filter(Boolean);
  if (bits.length) el.append(h("p", { class: "small muted" }, bits.join(" · ")));
  return { cleanup: quick.cleanup };
}

function itemCard(item) {
  return h("div", { class: "card" }, h("div", { class: "between" },
    h("div", { class: "grow" }, h("strong", {}, `${ICONS[item.kind] || "•"} `, item.link ? link(item.link, item.title) : item.title),
      item.body ? h("div", { class: "small muted", style: { whiteSpace: "pre-wrap" } }, item.body) : null),
    h("div", { class: "row" }, item.created_at ? h("span", { class: "small muted" }, ago(item.created_at)) : null,
      item.id ? h("button", { class: "link", onclick: (e) => busy(e.target, async () => {
        await post(`/api/inbox/${item.id}/dismiss`); e.target.closest(".card").remove();
      }) }, "Dismiss") : null)));
}

function dealAlerts(deals) {
  const box = h("div", { class: "card" });
  const list = h("div", { class: "list" }, ...deals.map((d) => h("div", { class: "list-row" },
    h("div", { class: "main" }, d.link ? link(d.link, d.title) : d.title, d.body ? h("div", { class: "small muted" }, d.body) : null),
    h("span", { class: "small muted side" }, ago(d.created_at)))));
  box.append(h("div", { class: "between" },
    h("strong", {}, `🔥 ${deals.length} stock-up deal${deals.length === 1 ? "" : "s"}`),
    h("div", { class: "row" }, link("#/deals", "See deals"),
      h("button", { class: "link", onclick: (e) => busy(e.target, async () => {
        await Promise.all(deals.filter((d) => d.id).map((d) => post(`/api/inbox/${d.id}/dismiss`)));
        box.remove();
      }) }, "Dismiss all"))),
    more("Show them", list));
  return box;
}
