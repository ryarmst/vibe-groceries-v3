// The No Frills cart: quantities, substitutions, and where each line came from.
import { get, post } from "../lib/api.js";
import { busy, empty, errorBox, h, link, money, toast } from "../lib/ui.js";

export async function render(el) {
  el.append(h("h1", {}, "Cart"));
  let cart;
  try { cart = await get("/api/cart"); } catch (e) { el.append(errorBox(e)); return; }
  el.append(h("p", { class: "small muted" }, `${cart.store_name || ""} — checkout happens on nofrills.ca; this app can't place orders.`,
    cart.store_mismatch ? h("strong", { class: "warn" }, " The cart's store differs from the app's store; prices may differ.") : null));
  if (!cart.lines.length) { el.append(empty("The cart is empty.")); return; }
  const rows = cart.lines.map((l) => {
    const qty = h("input", { type: "number", min: 0, step: l.selling_type === "SOLD_BY_WEIGHT" ? "any" : 1, value: l.quantity, style: { width: "5em" } });
    return h("tr", {},
      h("td", {}, h("strong", {}, `${l.brand ? l.brand + " " : ""}${l.name}`),
        h("div", { class: "small muted" }, l.from_run ? ["from ", link(`#/runs/${l.from_run.run_id}`, l.from_run.summary), l.from_run.requirement ? ` (${l.from_run.requirement})` : ""] : "added on nofrills.ca")),
      h("td", { class: "num" }, h("div", { class: "row" }, qty, h("button", { class: "link", onclick: (e) => busy(e.target, async () => {
        await post(`/api/cart/${l.product_id}`, { quantity: Number(qty.value) }); toast(Number(qty.value) ? "Updated." : "Removed."); location.reload();
      }) }, "Set"))),
      h("td", { class: "num" }, money(l.line_total), l.regular_line_total > l.line_total ? h("div", { class: "small muted", style: { textDecoration: "line-through" } }, money(l.regular_line_total)) : null),
      h("td", { class: "small" }, h("button", { class: "link", onclick: (e) => busy(e.target, async () => {
        const r = await post(`/api/cart/${l.product_id}/substitution`, { allowed: !l.substitution_allowed }); toast(r.message); location.reload();
      }) }, l.substitution_allowed ? "Subs OK" : "Subs off"), l.substitution_comment ? h("div", { class: "muted" }, `“${l.substitution_comment}”`) : null),
      h("td", {}, link(`#/product/${l.product_id}`, "Modify…")));
  });
  el.append(h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "Item"), h("th", { class: "num" }, "Qty"), h("th", { class: "num" }, "Total"), h("th", {}, "Subs"), h("th", {}))),
    h("tbody", {}, ...rows))),
    h("p", { style: { textAlign: "right" } }, `Subtotal ${money(cart.subtotal)} · savings ${money(cart.total_discounts)} · `, h("strong", {}, `estimated ${money(cart.estimated_total)}`)));
}
