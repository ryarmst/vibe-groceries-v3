import { get } from "../lib/api.js";
import { day, h, link, money } from "../lib/ui.js";

export async function render(el, [orderId]) {
  const o = await get(`/api/orders/${orderId}`);
  el.append(h("p", { class: "small" }, link("#/orders", "← Orders")), h("h1", {}, `Order ${o.order_id}`),
    h("p", { class: "muted" }, `${day(o.placed_at)} · ${o.store || ""} · ${money(o.total)} (${o.item_count} items, ${o.from_runs} from runs)`));
  const lists = [["Approved in the app but not bought", o.approved_in_app_but_not_bought.map((x) => `${x.product} (${x.requirement || ""})`)],
    ["Added on nofrills.ca", o.added_outside_app], ["Not fulfilled", o.not_fulfilled], ["Substituted", o.substituted.map((s) => `${s.name} → ${s.with}`)]];
  for (const [title, items] of lists) if (items.length) el.append(h("div", { class: "card" }, h("h3", {}, title), h("ul", {}, ...items.map((i) => h("li", {}, i)))));
  el.append(h("table", { class: "table" }, h("tbody", {}, ...o.items.map((i) => h("tr", {},
    h("td", {}, `${i.brand ? i.brand + " " : ""}${i.name}`, i.from_run ? h("div", { class: "small muted" }, link(`#/runs/${i.from_run.run_id}`, i.from_run.requirement || "from run")) : null),
    h("td", { class: "num" }, i.quantity), h("td", { class: "num" }, money(i.total)), h("td", { class: "small" }, i.fulfilled ? "" : "not fulfilled"))))));
}
