// One product: price history, cart line, swap to an alternative, quantity, substitution, remove, note.
import { get, post } from "../lib/api.js";
import { badge, busy, clear, empty, extLink, h, link, money, toast, unit, when } from "../lib/ui.js";

export async function render(el, [productId], query) {
  const qs = new URLSearchParams();
  if (query.sel) qs.set("selection_id", query.sel);
  if (query.q !== undefined) qs.set("q", query.q);
  const d = await get(`/api/products/${productId}/modify?${qs}`);
  const sel = d.selection;
  const inCart = !!d.cart_line;
  const back = sel ? `#/runs/${d.run.run_id}` : "#/cart";
  el.append(h("p", { class: "small" }, link(back, sel ? `← ${d.run.summary}` : "← Cart")));
  const cur = d.current || {};
  el.append(h("div", { class: "card" },
    h("div", { class: "product" }, d.product.image_url ? h("img", { src: d.product.image_url, alt: "" }) : h("div"),
      h("div", {}, h("div", { class: "small muted" }, d.product.brand || ""), h("h1", { style: { margin: 0 } }, d.product.name, " ", h("span", { class: "small muted" }, d.product.size || "")),
        h("div", { class: "small muted" }, d.product.category || ""),
        h("div", { class: "small" }, inCart ? `In cart × ${d.cart_line.quantity}` : sel ? `In run: ${sel.status}` : "Not in cart",
          d.cart_error ? h("span", { class: "warn" }, ` · ${d.cart_error}`) : null)),
      h("div", { class: "price" }, h("div", { class: "now" }, money(cur.price)), cur.regular_price && cur.price < cur.regular_price ? h("div", { class: "was" }, money(cur.regular_price)) : null,
        h("div", { class: "small muted" }, unit(cur.unit_price, cur.unit_price_basis)))),
    stats(d.stats), d.product.link ? extLink(d.product.link, "View on nofrills.ca") : null));

  const note = h("textarea", { rows: 2, placeholder: "Note for the assistant (optional), saved with whichever change you make" });
  const send = (action, extra = {}) => post("/api/modify", { action, product_id: productId, selection_id: sel?.selection_id || null,
    in_cart: inCart, note: note.value || null, ...extra });

  const qty = h("input", { type: "number", min: 0, step: d.product.sold_by === "weight" ? "any" : 1, value: d.cart_line?.quantity ?? sel?.quantity ?? 1, style: { width: "5em" } });
  const subOk = h("input", { type: "checkbox", checked: d.cart_line?.substitution_allowed ?? sel?.substitution_allowed ?? false });
  const subNote = h("input", { value: d.cart_line?.substitution_comment || sel?.substitution_note || "", placeholder: "OK substitute? e.g. any brand", style: { flex: 1 } });
  el.append(h("div", { class: "card" }, h("h2", { style: { marginTop: 0 } }, "Change"),
    h("div", { class: "row" }, h("label", { class: "row" }, "Quantity", qty), h("button", { onclick: (e) => busy(e.target, async () => {
      const r = await send("quantity", { quantity: Number(qty.value) }); toast(r.message); location.reload();
    }) }, "Set quantity"), h("span", { class: "spacer" }),
      sel?.status === "rejected" ? h("button", { class: "primary", onclick: (e) => busy(e.target, async () => { const r = await send("restore"); toast(r.message); location.reload(); }) }, "Add back")
        : h("button", { class: "danger", onclick: (e) => busy(e.target, async () => { const r = await send("remove"); toast(r.message); location.hash = back; }) }, inCart ? "Remove from cart" : "Remove")),
    h("div", { class: "row small", style: { marginTop: "8px" } }, h("label", { class: "row" }, subOk, "Substitutes OK"), subNote,
      h("button", { onclick: (e) => busy(e.target, async () => {
        const r = await send("substitution", { sub_allowed: subOk.checked, sub_note: subNote.value || null }); toast(r.message);
      }) }, "Save")),
    h("div", { style: { marginTop: "8px" } }, note),
    h("div", { class: "row", style: { marginTop: "6px" } }, h("button", { onclick: (e) => busy(e.target, async () => { const r = await send("note"); toast(r.message || "Saved."); }) }, "Save note only"))));

  const search = h("input", { value: d.query, style: { flex: 1 } });
  const list = h("div");
  if (d.search_error) list.append(h("p", { class: "warn" }, d.search_error));
  if (!d.alternatives.length) list.append(empty("No alternatives found."));
  for (const a of d.alternatives) {
    list.append(h("div", { class: "between", style: { padding: "6px 0", borderBottom: "1px solid var(--line)" } },
      h("div", {}, h("strong", {}, `${a.brand ? a.brand + " " : ""}${a.name}`), " ", h("span", { class: "small muted" }, a.package_size || ""),
        a.past_feedback?.removed ? badge("removed before", "warn") : null,
        h("div", { class: "small muted" }, [a.deal, a.availability && a.availability !== "OK" ? a.availability : null].filter(Boolean).join(" · "))),
      h("div", { class: "row" }, h("span", { class: "num" }, money(a.price), h("br"), h("span", { class: "small muted" }, unit(a.unit_price, a.unit_price_basis))),
        h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
          const r = await send("swap", { new_product_id: a.product_id, quantity: Number(qty.value) || null }); toast(r.message); location.hash = back;
        }) }, inCart ? "Swap in cart" : "Swap"),
        h("button", { onclick: (e) => busy(e.target, async () => {
          const r = await send("substitute_with", { new_product_id: a.product_id, sub_note: subNote.value || null }); toast(r.message);
        }) }, "Use as substitute"))));
  }
  el.append(h("div", { class: "card" }, h("h2", { style: { marginTop: 0 } }, "Alternatives"),
    h("div", { class: "row" }, search, h("button", { onclick: () => { location.hash = `#/product/${productId}?${new URLSearchParams({ ...(query.sel ? { sel: query.sel } : {}), q: search.value })}`; } }, "Search")),
    list));

  if (d.options?.length) {
    el.append(h("div", { class: "card" }, h("h2", { style: { marginTop: 0 } }, "Other options in this run"),
      ...d.options.map((o) => h("div", { class: "between" }, `${o.product.brand || ""} ${o.product.name}`, money(o.price?.price)))));
  }
  el.append(h("h2", {}, "History"), h("div", {}, ...(d.events.length ? d.events.map((e) => h("div", { class: "small", style: { padding: "4px 0" } },
    h("span", { class: "muted" }, when(e.time)), " · ", h("strong", {}, e.title), e.detail ? ` — ${e.detail}` : "", e.note ? h("span", { class: "muted" }, ` “${e.note}”`) : null))
    : [empty("No history yet.")])));
}

function stats(st) {
  if (!st || !st.observations) return h("p", { class: "small muted" }, "No price history yet.");
  return h("p", { class: "small muted" }, [`seen ${st.days_observed} day(s)`, st.min_price !== undefined ? `lowest ${money(st.min_price)}` : null,
    st.avg_90d ? `90-day avg ${money(st.avg_90d)}` : null, st.is_lowest_observed ? "lowest we've seen" : null,
    st.package_size_changed ? "package size changed" : null].filter(Boolean).join(" · "));
}
