// Past No Frills orders.
import { get } from "../lib/api.js";
import { day, empty, h, money } from "../lib/ui.js";

export async function render(el) {
  const { orders, error } = await get("/api/orders");
  el.append(h("h1", {}, "Orders"), error ? h("p", { class: "small warn" }, error) : null);
  if (!orders.length) { el.append(empty("No orders yet.")); return; }
  el.append(h("div", { class: "card list" }, ...orders.map((o) => h("a", { class: "list-row", href: `#/orders/${o.order_id}` },
    h("div", { class: "main" }, h("div", { class: "title" }, day(o.placed_at)),
      h("div", { class: "small muted" }, [o.fulfillment, `#${o.order_id}`].filter(Boolean).join(" · "))),
    h("div", { class: "side" }, h("div", {}, money(o.total)), o.imported ? null : h("div", { class: "small muted" }, "not loaded yet"))))));
}
