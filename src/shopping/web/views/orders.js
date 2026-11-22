import { get } from "../lib/api.js";
import { day, empty, h, link, money } from "../lib/ui.js";

export async function render(el) {
  const { orders, error } = await get("/api/orders");
  el.append(h("h1", {}, "Orders"), error ? h("p", { class: "small warn" }, error) : null);
  if (!orders.length) { el.append(empty("No orders yet.")); return; }
  el.append(h("table", { class: "table" }, h("tbody", {}, ...orders.map((o) => h("tr", {},
    h("td", {}, link(`#/orders/${o.order_id}`, o.order_id)), h("td", {}, day(o.placed_at)), h("td", { class: "num" }, money(o.total)),
    h("td", { class: "small muted" }, o.fulfillment || ""), h("td", { class: "small" }, o.imported ? "imported" : "not imported"))))));
}
