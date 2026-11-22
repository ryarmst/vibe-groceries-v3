import { get } from "../lib/api.js";
import { empty, h, link, money, when } from "../lib/ui.js";

const LABEL = { review: "In review", building: "Building", partially_approved: "Partly approved", approved: "Approved", applied: "In cart", cancelled: "Cancelled" };

export async function render(el) {
  const { runs } = await get("/api/runs");
  el.append(h("h1", {}, "Shopping runs"));
  if (!runs.length) { el.append(empty("No runs yet. Ask the assistant: “do a grocery shop”.")); return; }
  el.append(h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Run"), h("th", {}, "Status"), h("th", { class: "num" }, "Items"), h("th", { class: "num" }, "Estimate"), h("th", {}, "Created"))),
    h("tbody", {}, ...runs.map((r) => h("tr", {},
      h("td", {}, link(`#/runs/${r.run_id}`, r.summary)), h("td", {}, h("span", { class: `status ${r.status}` }, LABEL[r.status] || r.status)),
      h("td", { class: "num" }, r.item_count), h("td", { class: "num" }, money(r.estimated_total)), h("td", { class: "small" }, when(r.created_at))))))));
}
