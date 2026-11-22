// One shopping run: review cards. Approving puts items in the cart in the background.
import { get, post } from "../lib/api.js";
import { badge, busy, empty, h, link, money, more, pct, poll, toast, unit, when } from "../lib/ui.js";

const STATUS = { pending: "Pending", approved: "Approved · not in cart", applied: "In cart", rejected: "Removed", failed: "Cart error", adding: "Adding to cart…" };
const REASONS = { grocery_list: "List", pantry_low: "Low", regular_staple: "Regular", good_deal: "Deal", preferred_product: "Preferred",
  preferred_brand: "Brand", user_requested: "Asked for", alternative: "Alternative" };

export async function render(el, [runId]) {
  let stopWatching = null;
  // Redraw from the server without losing your place on the page.
  async function draw() {
    const run = await get(`/api/runs/${runId}`);
    const y = window.scrollY;
    el.replaceChildren();
    el.append(h("p", { class: "small" }, link("#/runs", "← All runs")),
      h("div", { class: "between" }, h("h1", {}, run.summary), h("span", { class: `status ${run.status}` }, run.status.replace("_", " "))),
      run.notes ? h("p", { class: "muted small" }, run.notes) : null);
    const counts = run.counts;
    el.append(h("p", { class: "small muted" }, Object.entries(counts).map(([k, v]) => `${v} ${STATUS[k] || k}`).join(" · ")));
    for (const section of run.sections) {
      const cards = run.cards.filter((c) => c.section === section.key);
      if (!cards.length) continue;
      el.append(h("h2", {}, section.title));
      for (const c of cards) el.append(card(run, c, draw));
    }
    if (!run.cards.length) el.append(empty("This run is empty."));
    const pending = run.pending_snapshot;
    el.append(h("div", { class: "bottom-bar" }, h("div", { class: "inner" },
      h("strong", { class: "total" }, money(run.estimated_total), h("span", { class: "small muted" }, " before tax")),
      h("span", { class: "spacer" }),
      run.editable && pending.length ? h("button", { class: "danger", onclick: (e) => busy(e.target, async () => {
        if (!confirm(`Remove all ${pending.length} items still waiting for a decision?`)) return;
        await post(`/api/runs/${runId}/reject-remaining`, { snapshot: pending }); await draw();
      }, e.target.parentElement) }, "Remove the rest") : null,
      run.editable && pending.length ? h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
        const r = await post(`/api/runs/${runId}/approve-all`, { snapshot: pending });
        toast([`Approved ${r.approved}.`, r.message].filter(Boolean).join(" ")); await draw(); watch();
      }, e.target.parentElement) }, `Approve all ${pending.length}`) : null,
      (counts.approved || counts.failed) ? h("button", { onclick: (e) => busy(e.target, async () => {
        const r = await post(`/api/runs/${runId}/apply`); toast(r.message); await draw(); watch();
      }, e.target.parentElement) }, "Add approved to cart") : null)));
    window.scrollTo(0, y);
    return run;
  }

  // Approved items go into the cart in the background; follow along until they're all in.
  let lastEvent = 0;
  function watch() {
    if (stopWatching) return;
    stopWatching = poll(async () => {
      const inbox = await get(`/api/inbox?after_event=${lastEvent}`);
      for (const ev of inbox.cart.events.filter((e) => e.run_id === runId)) toast(ev.message, ev.ok ? "ok" : "err");
      lastEvent = inbox.cart.last_event_id;
      if (inbox.cart.pending) return false;
      stopWatching = null;
      await draw();
      return true;
    }, 1500);
  }

  const inbox = await get("/api/inbox");
  lastEvent = inbox.cart.last_event_id;
  const run = await draw();
  if (inbox.cart.pending || run.counts.adding) watch();
  return { cleanup: () => stopWatching?.() };
}

function priceBlock(p) {
  if (!p) return h("div");
  const sale = p.regular_price && p.price < p.regular_price;
  return h("div", { class: "price" }, h("div", { class: `now ${sale ? "sale" : ""}` }, money(p.price)),
    sale ? h("div", { class: "was" }, money(p.regular_price)) : null, h("div", { class: "small muted" }, unit(p.unit_price, p.unit_price_basis)));
}

function historyLine(sel) {
  const bits = [];
  const st = sel.history;
  if (st?.min_price !== undefined && st.days_observed > 1) bits.push(`lowest seen ${money(st.min_price)}`);
  if (st?.avg_90d) bits.push(`90-day avg ${money(st.avg_90d)}`);
  if (st?.is_lowest_observed) bits.push("lowest we've seen");
  if (sel.price?.deal) bits.push(sel.price.deal + (sel.price.deal_ends ? ` (ends ${sel.price.deal_ends})` : ""));
  const req = sel.requirement_history;
  if (req?.last_purchased) bits.push(`last bought ${req.last_purchased.days_ago} days ago`);
  else if (req?.last_added) bits.push(`last added ${req.last_added.days_ago} days ago`);
  if (req?.last_removed?.note) bits.push(`last removed: “${req.last_removed.note}”`);
  return bits.length ? h("div", { class: "small muted" }, bits.slice(0, 3).join(" · ")) : null;
}

function product(sel) {
  const p = sel.product;
  return h("div", { class: "product" },
    p.image_url ? h("img", { src: p.image_url, alt: "", loading: "lazy" }) : h("div"),
    h("div", {}, h("div", { class: "small muted" }, p.brand || ""), h("div", { class: "name" }, p.name, " ", h("span", { class: "muted small" }, p.size || "")),
      historyLine(sel)),
    priceBlock(sel.price));
}

function card(run, c, redraw) {
  const sel = c.main;
  const box = h("section", { class: "card", id: `card-${sel.selection_id}` });
  box.append(h("div", { class: "between" }, h("h3", {}, c.label), h("span", { class: `status ${sel.status}` }, STATUS[sel.status] || sel.status)));
  box.append(product(sel));
  box.append(h("div", { style: { marginTop: "6px" } }, ...(sel.reasons || []).map((r) => badge(REASONS[r] || r, r === "good_deal" ? "sale" : "")),
    sel.price?.regular_price && sel.price.price < sel.price.regular_price ? badge(pct((1 - sel.price.price / sel.price.regular_price) * 100), "sale") : null));
  if (sel.rationale) box.append(h("p", { class: "small" }, sel.rationale));
  if (sel.past_feedback?.last_note) box.append(h("p", { class: "small warn" }, `Earlier: “${sel.past_feedback.last_note}”`));
  for (const f of c.feedback) box.append(h("p", { class: "small muted" }, `✎ ${f.action.replace("_", " ")}${f.note ? `: “${f.note}”` : ""}`));
  if (sel.substitution_allowed || sel.substitution_note) box.append(h("p", { class: "small muted" }, `⇄ Substitutes ${sel.substitution_allowed ? "OK" : "off"}${sel.substitution_note ? `: “${sel.substitution_note}”` : ""}`));
  if (sel.last_error) box.append(h("p", { class: "small bad" }, `Cart: ${sel.last_error}`));

  if (run.editable && sel.status === "pending") {
    let option = sel.selection_id;
    if (c.kind === "group" && c.options.length > 1) {
      const opts = h("div", { class: "options" }, h("div", { class: "small muted" }, "Choose one, then Approve"));
      for (const o of c.options) {
        const radio = h("input", { type: "radio", name: `opt-${c.id}`, value: o.selection_id, checked: o.selection_id === sel.selection_id,
          onchange: () => { option = o.selection_id; } });
        opts.append(h("label", { class: "option" }, radio,
          h("span", {}, `${o.product.brand || ""} ${o.product.name} ${o.product.size || ""}`.trim(),
            o.selection_id === c.recommended_selection_id ? badge("Recommended", "ok") : null,
            o.rationale ? h("div", { class: "small muted" }, o.rationale) : null),
          h("span", { class: "small num" }, money(o.price?.price), h("br"), h("span", { class: "muted" }, unit(o.price?.unit_price, o.price?.unit_price_basis)))));
      }
      box.append(opts);
    }
    const subOk = h("input", { type: "checkbox", checked: sel.substitution_allowed });
    const subNote = h("input", { value: sel.substitution_note || "", placeholder: "Which substitutes are OK? e.g. any brand, same size", maxLength: 250, class: "grow" });
    const note = h("textarea", { rows: 2, placeholder: "Note for the assistant, e.g. “still have two jars” or “prefer the sourdough”" });
    const weight = sel.product.sold_by === "weight";
    const qty = h("input", { type: "number", value: sel.quantity, min: weight ? 0.1 : 1, step: weight ? "any" : 1, style: { width: "4.5em" }, "aria-label": "Quantity" });
    const decide = (decision) => (e) => busy(e.target, async () => {
      const r = await post(`/api/runs/${run.run_id}/selections/${sel.selection_id}/triage`, {
        decision, option: option !== sel.selection_id ? option : null, note: note.value || null, quantity: Number(qty.value) || null,
        sub_allowed: subOk.checked, sub_note: subNote.value || null });
      if (r.message) toast(r.message, r.needs_session ? "err" : "ok");
      await redraw();
    });
    box.append(
      h("div", { class: "row", style: { marginTop: "10px" } }, h("label", { class: "row" }, "Qty", qty),
        h("span", { class: "spacer" }),
        h("button", { class: "danger", onclick: decide("remove") }, "Remove"),
        h("button", { class: "primary", onclick: decide("keep") }, "Approve")),
      more(sel.substitution_allowed || sel.substitution_note ? "Note, substitutes or swap (substitutes on)" : "Note, substitutes or swap",
        note,
        h("div", { class: "row small" }, h("label", { class: "row" }, subOk, "Substitutes OK"), subNote),
        link(`#/product/${sel.product.product_id}?sel=${sel.selection_id}`, "Swap for a different product…")));
  } else {
    box.append(h("div", { class: "row small muted", style: { marginTop: "8px" } }, `Qty ${sel.quantity}`,
      sel.suggested_quantity && sel.suggested_quantity !== sel.quantity ? ` (suggested ${sel.suggested_quantity})` : "",
      sel.applied_to_cart_at ? ` · added ${when(sel.applied_to_cart_at)}` : "", h("span", { class: "spacer" }),
      link(`#/product/${sel.product.product_id}?sel=${sel.selection_id}`, "Modify…")));
  }
  return box;
}
