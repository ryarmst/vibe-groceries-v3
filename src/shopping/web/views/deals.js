// Deals: Today's top deals (assessed daily, or now with the button), your items on sale, best by aisle.
// Every item can go straight into the cart.
import { get, post } from "../lib/api.js";
import { ago, badge, busy, clear, empty, h, link, money, toast, unit } from "../lib/ui.js";

export async function render(el) {
  let poll = null;
  async function draw() {
    const [ctx, top, status] = await Promise.all([get("/api/deals/context"), get("/api/deals/top?include_closed=false"), get("/api/deals/assess")]);
    clear(el);
    const scan = ctx.deal_data;
    const assessBtn = h("button", { class: "primary", disabled: status.running, onclick: (e) => busy(e.target, async () => {
      const r = await post("/api/deals/assess");
      toast(r.message);
      watch();
      await draw();
    }) }, status.running ? "Assessing…" : "Assess deals");
    el.append(h("div", { class: "between" }, h("h1", {}, "Deals"),
      h("div", { class: "row" }, h("span", { class: "small muted" },
        status.running ? "Checking prices and picking the best deals…"
          : scan.last_scan ? `${scan.deals_known_now} items on sale · checked ${ago(scan.last_scan.finished_at)}` : "Not checked yet"),
        assessBtn)));

    el.append(h("h2", {}, "Today's top deals"),
      h("p", { class: "small muted" }, "Standout prices on things you buy, compared with what you've paid before, plus the assistant's picks ",
        "of great deals on things you haven't tried. Updated every morning."));
    if (!top.deals.length) el.append(empty(status.running ? "Working on it…" : "No standout deals right now. Try Assess deals."));
    for (const d of top.deals) el.append(topDeal(d, draw));

    el.append(h("h2", {}, "Your items on sale"));
    if (!ctx.your_items_on_sale.length) el.append(empty("Nothing you've bought before is on sale right now."));
    el.append(h("div", { class: "card" }, ...ctx.your_items_on_sale.map((p) => productRow(p))));

    el.append(h("h2", {}, "Best by aisle"));
    for (const [aisle, items] of Object.entries(ctx.top_deals_by_aisle)) {
      el.append(h("details", { class: "card" }, h("summary", {}, h("strong", {}, aisle), h("span", { class: "muted small" }, ` · ${items.length}`)),
        ...items.map((p) => productRow(p))));
    }
    el.append(addOwn(draw));
  }

  function watch() {
    if (poll) return;
    poll = setInterval(async () => {
      const s = await get("/api/deals/assess").catch(() => null);
      if (s && !s.running) { clearInterval(poll); poll = null; toast("Deals assessed."); draw(); }
    }, 5000);
  }

  await draw();
  const s = await get("/api/deals/assess");
  if (s.running) watch();
  return { cleanup: () => poll && clearInterval(poll) };
}

function addToCart(productId, suggested = 1) {
  const qty = h("input", { type: "number", min: 1, step: 1, value: suggested, style: { width: "4em" }, title: "Quantity" });
  return h("span", { class: "row", style: { gap: "4px" } }, qty, h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
    const r = await post(`/api/cart/${productId}/add`, { quantity: Number(qty.value) || 1 });
    toast(r.message);
  }) }, "Add to cart"));
}

function productRow(p) {
  return h("div", { class: "between", style: { padding: "8px 0", borderBottom: "1px solid var(--line)" } },
    h("div", { style: { flex: 1, minWidth: "220px" } }, h("strong", {}, `${p.brand ? p.brand + " " : ""}${p.name}`), " ",
      h("span", { class: "small muted" }, p.size || ""),
      h("div", { class: "small muted" }, [p.deal, p.deal_ends ? `ends ${p.deal_ends}` : null, p.is_lowest_seen ? "lowest we've seen" : null,
        p.bought_before ? `bought ${p.bought_before.times}×` : null, p.other_variants_same_price ? `+${p.other_variants_same_price} other flavours` : null]
        .filter(Boolean).join(" · "), " ", link(`#/product/${p.product_id}`, "details"))),
    h("div", { class: "price", style: { minWidth: "90px" } }, h("span", { class: "sale" }, money(p.price)),
      p.regular_price ? h("span", { class: "was" }, " ", money(p.regular_price)) : null, h("div", { class: "small muted" }, unit(p.unit_price, p.unit_price_basis))),
    addToCart(p.product_id));
}

function topDeal(d, redraw) {
  const note = h("input", { placeholder: "Why not? (optional)", style: { flex: 1, minWidth: "160px" } });
  const who = { auto: "daily check", review: "assistant's pick", agent: "assistant", user: "you" }[d.added_by] || d.added_by;
  return h("section", { class: "card" },
    h("div", { class: "between" }, h("h3", {}, d.relevant_to || d.aisle || "Deal"),
      h("span", { class: "row small" }, h("span", { class: "muted" }, `from ${who}`), h("span", { class: `badge ${d.rating === 3 ? "sale" : "ok"}` }, "★".repeat(d.rating), " ", d.rating_label))),
    h("div", { class: "product" }, d.image_url ? h("img", { src: d.image_url, alt: "" }) : h("div"),
      h("div", {}, h("div", { class: "small muted" }, d.brand || ""), h("div", { class: "name" }, d.name, " ", h("span", { class: "small muted" }, d.size || "")),
        badge(d.headline, "sale"), h("div", { class: "small" }, d.why || ""),
        h("div", { class: "small muted" }, [d.deal_text, d.ends ? `ends ${d.ends}` : null].filter(Boolean).join(" · ")),
        d.price_changed_since_flagged ? h("div", { class: "small warn" }, `Price changed: now ${money(d.latest_price)}`) : null),
      h("div", { class: "price" }, h("div", { class: "now sale" }, money(d.price)), d.discount_pct ? h("div", { class: "was" }, money(d.regular_price)) : null,
        h("div", { class: "small muted" }, unit(d.unit_price, d.unit_price_basis)))),
    h("div", { class: "row", style: { marginTop: "8px" } }, addToCart(d.product_id, d.suggested_quantity || 1),
      h("span", { class: "spacer" }), note,
      h("button", { onclick: (e) => busy(e.target, async () => {
        await post(`/api/deals/top/${d.deal_id}/dismiss`, { note: note.value || null }); toast("Dismissed; it won't come back for 60 days."); redraw();
      }) }, "Not for me")));
}

function addOwn(redraw) {
  const product = h("input", { placeholder: "nofrills.ca product link or code", style: { width: "100%" } });
  const why = h("input", { placeholder: "Why (optional)" });
  return h("details", { class: "card" }, h("summary", {}, "Add a deal yourself"),
    h("div", { class: "form-grid", style: { marginTop: "8px" } }, h("label", {}, "Product", product), h("label", {}, "Why", why)),
    h("div", { class: "row", style: { marginTop: "8px" } }, h("button", { onclick: (e) => busy(e.target, async () => {
      await post("/api/deals/top", { product: product.value, why: why.value || null });
      toast("Added to Today's top deals."); redraw();
    }) }, "Add deal")));
}
