// Shopping runs: what the assistant put together for review, newest first.
import { get } from "../lib/api.js";
import { empty, h, money, when } from "../lib/ui.js";

const LABEL = { review: "To review", building: "Being built", partially_approved: "Partly approved", approved: "Approved", applied: "In cart", cancelled: "Cancelled" };

export async function render(el) {
  const { runs } = await get("/api/runs");
  el.append(h("h1", {}, "Shopping runs"));
  if (!runs.length) { el.append(empty("No runs yet. In Chat, choose “Plan a shop to review”.")); return; }
  el.append(h("div", { class: "card list" }, ...runs.map((r) => h("a", { class: "list-row", href: `#/runs/${r.run_id}` },
    h("div", { class: "main" }, h("div", { class: "title" }, r.summary),
      h("div", { class: "small muted" }, `${r.item_count} items · ${when(r.created_at)}`)),
    h("div", { class: "side" }, h("span", { class: `status ${r.status}` }, LABEL[r.status] || r.status),
      h("div", { class: "small" }, money(r.estimated_total)))))));
}
