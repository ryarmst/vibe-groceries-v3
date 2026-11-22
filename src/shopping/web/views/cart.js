// The No Frills cart: what's in it, where each item came from, and quick quantity changes.
import { get, post } from "../lib/api.js";
import { busy, empty, h, link, money, toast } from "../lib/ui.js";

export async function render(el) {
  el.append(h("h1", {}, "Cart"));
  const cart = await get("/api/cart");
  el.append(h("p", { class: "small muted" }, `${cart.store_name || ""}. You check out on nofrills.ca; this app can't place orders.`,
    cart.store_mismatch ? h("strong", { class: "warn" }, " This cart is for a different store than the app's, so prices may differ.") : null));
  if (!cart.lines.length) { el.append(empty("The cart is empty.")); return; }
  const list = h("div", { class: "card list" });
  for (const l of cart.lines) list.append(line(l));
  el.append(list, h("p", { class: "between" }, h("span", { class: "small muted" }, `Subtotal ${money(cart.subtotal)} · you save ${money(cart.total_discounts)}`),
    h("strong", {}, `About ${money(cart.estimated_total)}`)));
}

function line(l) {
  const weight = l.selling_type === "SOLD_BY_WEIGHT";
  const row = h("div", { class: "list-row lock" });
  const qty = h("input", { type: "number", min: 0, step: weight ? "any" : 1, value: l.quantity, style: { width: "4.5em" }, "aria-label": "Quantity" });
  const set = (value) => busy(row.querySelector("button"), async () => {
    const r = await post(`/api/cart/${l.product_id}`, { quantity: value });
    if (!value) { row.remove(); toast("Removed."); return; }
    qty.value = r.quantity ?? value;
    toast("Updated.");
  }, row);
  const sale = l.regular_line_total > l.line_total;
  row.append(
    h("div", { class: "main" }, h("div", { class: "title" }, `${l.brand ? l.brand + " " : ""}${l.name}`),
      h("div", { class: "small muted" }, l.from_run ? ["from ", link(`#/runs/${l.from_run.run_id}`, l.from_run.summary)] : "added on nofrills.ca",
        l.substitution_allowed ? " · substitutes OK" : "")),
    h("div", { class: "side" }, h("div", { class: sale ? "sale" : "" }, money(l.line_total)),
      sale ? h("div", { class: "small muted", style: { textDecoration: "line-through" } }, money(l.regular_line_total)) : null),
    h("div", { class: "row", style: { width: "100%", gap: "6px" } }, qty,
      h("button", { onclick: () => set(Number(qty.value)) }, "Update"),
      h("button", { class: "danger", onclick: () => set(0) }, "Remove"),
      h("span", { class: "spacer" }), link(`#/product/${l.product_id}`, "Swap or more…")));
  return row;
}
