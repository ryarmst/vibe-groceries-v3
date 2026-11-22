// Home: Quick Shop, the inbox (what needs a person), and session / deal-scan / cart status.
import { get, post } from "../lib/api.js";
import { ago, busy, empty, h, link, toast } from "../lib/ui.js";
import { quickBox } from "./quick.js";

const ICONS = { needs_session: "🔌", run_review: "🧾", plan_ready: "🍽️", meal_checkin: "👩‍🍳", meal_skipped: "⏭️",
  order_imported: "📦", job_failed: "⚠️", deal_alert: "🔥" };

export async function render(el) {
  const inbox = await get("/api/inbox");
  el.append(h("div", { class: "between" }, h("h1", {}, `Hi ${inbox.user.name.split(" ")[0]}`),
    h("div", { class: "row" },
      link("#/chat", "💬 Chat"),
      h("button", { onclick: (e) => busy(e.target, async () => {
        const r = await post("/api/meals/plan-week");
        toast(r.message, r.started ? "ok" : "err");
      }) }, "Plan the week now"))));
  const s = inbox.retailer_session;
  el.append(h("p", { class: "muted small" },
    s.state === "connected" ? "No Frills account connected. " : h("span", {}, h("strong", { class: "warn" }, "No Frills isn't signed in: "),
      link("#/settings", "sign in"), ". "),
    "Deals scanned ", inbox.deal_scan.last ? ago(inbox.deal_scan.last.finished_at) : "never", inbox.deal_scan.running ? " (scanning now)" : "",
    inbox.cart.pending ? ` · adding ${inbox.cart.pending} item(s) to the cart…` : ""));
  el.append(h("section", { class: "card" }, h("h2", { style: { marginTop: 0 } }, "⚡ Quick Shop"), quickBox({ compact: true })));
  el.append(h("h2", {}, "Needs you"));
  if (!inbox.items.length) el.append(empty("Nothing waiting. Ask the assistant to shop or plan the week."));
  for (const item of inbox.items) {
    el.append(h("div", { class: "card" }, h("div", { class: "between" },
      h("div", {}, h("strong", {}, `${ICONS[item.kind] || "•"} `, item.link ? link(item.link, item.title) : item.title),
        item.body ? h("div", { class: "small muted", style: { whiteSpace: "pre-wrap" } }, item.body) : null),
      h("div", { class: "row" }, item.created_at ? h("span", { class: "small muted" }, ago(item.created_at)) : null,
        item.id ? h("button", { class: "link", onclick: async (e) => { await post(`/api/inbox/${item.id}/dismiss`); e.target.closest(".card").remove(); } }, "Dismiss") : null))));
  }
}
