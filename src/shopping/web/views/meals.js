// This week's suggested meals (Notion): keep, reject, cooked (+ rating, leftovers, note), skip, promote.
import { get, post } from "../lib/api.js";
import { badge, busy, day, empty, extLink, h, link, toast } from "../lib/ui.js";

export async function render(el, _params, query) {
  const data = await get(`/api/meals${query.all ? "?all_weeks=true" : ""}`);
  el.append(h("div", { class: "between" }, h("h1", {}, query.all ? "All suggested meals" : `Meals · week of ${day(data.week)}`),
    h("div", { class: "row" },
      h("a", { href: query.all ? "#/meals" : "#/meals?all=1" }, query.all ? "This week" : "All weeks"),
      h("button", { class: "primary", onclick: (e) => busy(e.target, async () => {
        const r = await post("/api/meals/plan-week"); toast(r.message, r.started ? "ok" : "err");
      }) }, "Plan the week now"))));
  if (!data.configured) {
    el.append(h("div", { class: "flash err" }, data.error, " ", link("#/settings", "Settings")));
    return;
  }
  if (!data.meals.length) { el.append(empty("No meals suggested for this week yet.")); return; }
  for (const m of data.meals) el.append(mealCard(m));
}

function mealCard(m) {
  const box = h("section", { class: "card" });
  const status = m.Status || "Suggested";
  box.append(h("div", { class: "between" },
    h("div", {}, h("strong", { style: { fontSize: "17px" } }, m.Name || "Untitled"), " ", m.url ? extLink(m.url, "open in Notion") : null),
    h("span", { class: `status ${status}` }, status)));
  const facts = [m["Cook By"] ? `cook by ${day(m["Cook By"])}` : null, m.Effort, m.Servings ? `serves ${m.Servings}` : null,
    m["Main Ingredient"], m.Course].filter(Boolean);
  box.append(h("div", { class: "small muted" }, facts.join(" · ")));
  box.append(h("div", { style: { marginTop: "4px" } }, ...(m.Tags || []).map((t) => badge(t, t === "New" ? "warn" : "")),
    ...[].concat(m.Cuisine || []).map((c) => badge(c))));
  if (m.Feedback) box.append(h("p", { class: "small", style: { whiteSpace: "pre-wrap" } }, "✎ ", m.Feedback));

  const note = h("input", { placeholder: "Note (optional): how it went, what to change, why not…", style: { flex: 1, minWidth: "220px" } });
  const act = (action) => (e) => busy(e.target, async () => {
    await post(`/api/meals/${m.id}/decision`, { action, feedback: note.value || null });
    toast(`${m.Name}: ${{ keep: "planned", reject: "rejected", cooked: "cooked", skip: "skipped" }[action]}.`);
    location.reload();
  });
  if (status === "Suggested") {
    box.append(h("div", { class: "row", style: { marginTop: "8px" } }, note,
      h("button", { class: "danger", onclick: act("reject") }, "Reject"), h("button", { class: "primary", onclick: act("keep") }, "Keep")));
  } else if (status === "Planned") {
    box.append(h("div", { class: "row", style: { marginTop: "8px" } }, note,
      h("button", { onclick: act("skip") }, "Didn't get to it"), h("button", { class: "primary", onclick: act("cooked") }, "Cooked")));
  } else if (status === "Cooked") {
    box.append(h("div", { class: "row", style: { marginTop: "8px" } }, h("span", { class: "small muted" }, "A keeper?"),
      h("button", { onclick: (e) => busy(e.target, async () => {
        const r = await post(`/api/meals/${m.id}/promote`);
        toast(r.already_promoted ? "Already in Cooking." : "Saved to Cooking."); location.reload();
      }) }, "Promote to Cooking")));
  }
  return box;
}
