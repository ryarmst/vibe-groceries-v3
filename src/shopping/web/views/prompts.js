// Prompts: everything the assistant is told, grouped, with an editor. Edits are stored as the household's
// version (with history); Reset goes back to the default text that ships with the app.
import { del, get, put } from "../lib/api.js";
import { ago, badge, busy, clear, h, link, toast } from "../lib/ui.js";

export async function render(el, [pid]) {
  return pid ? editor(el, pid) : list(el);
}

async function list(el) {
  const { prompts, groups } = await get("/api/prompts");
  el.append(h("h1", {}, "Prompts"),
    h("p", { class: "muted" }, "Everything the assistant is told, in plain text. Change the wording, add house rules, or describe ",
      "your household; your version is used from the next message on. Reset brings back the original at any time."));
  for (const group of groups) {
    el.append(h("h2", {}, group));
    for (const p of prompts.filter((x) => x.group === group)) {
      el.append(h("a", { class: "card prompt-row", href: `#/prompts/${p.id}` },
        h("div", { class: "between" },
          h("strong", {}, p.title),
          h("span", {}, p.customized ? badge(`edited${p.updated_by ? ` by ${p.updated_by}` : ""} ${ago(p.updated_at)}`, "ok") : null,
            p.tier ? badge(p.tier === "smart" ? "smart model" : "fast model") : null,
            p.id === "about" && !p.chars ? badge("empty: add yours", "warn") : null)),
        h("div", { class: "small muted" }, p.description)));
    }
  }
}

async function editor(el, pid) {
  let d = await get(`/api/prompts/${pid}`);
  let saved = d.body;
  const text = h("textarea", { class: "mono", spellcheck: true, rows: 24, placeholder: d.id === "about" ?
    "e.g.\n- Two adults; Sam is vegetarian on weekdays.\n- We love Thai and Mexican; no mushrooms.\n- Instant Pot, air fryer, big freezer.\n- Weeknight dinners under 40 minutes." : "" });
  text.value = d.body;
  const count = h("span", { class: "small muted" });
  const state = h("span", { class: "small" });
  const tier = d.default_tier ? h("select", {}, h("option", { value: "fast" }, "Fast model"), h("option", { value: "smart" }, "Smart model")) : null;
  if (tier) tier.value = d.tier;
  const warnings = h("div");
  const previewBox = h("div");
  const history = h("div");
  const meta = h("div", { class: "small muted" });

  const dirty = () => text.value.trim() !== saved.trim() || (tier && tier.value !== d.tier);
  const refresh = () => {
    count.textContent = `${text.value.length.toLocaleString()} characters (~${Math.round(text.value.length / 4).toLocaleString()} tokens)`;
    state.textContent = dirty() ? "● unsaved changes" : "";
    state.className = `small ${dirty() ? "warn" : ""}`;
  };
  const drawMeta = () => {
    clear(meta).append(d.customized ? `Your version, saved by ${d.updated_by || "someone"} ${ago(d.updated_at)}.` : "The original text (not edited).");
    clear(warnings).append(...d.warnings.map((w) => h("div", { class: "flash err" }, w,
      ". Only the placeholders listed above are filled in; anything else is sent as typed.")));
    clear(history);
    if (d.revisions.length) {
      history.append(h("h2", {}, "Earlier versions"), h("div", { class: "table-wrap" }, h("table", { class: "table small" }, h("tbody", {},
        ...d.revisions.map((r) => h("tr", {},
          h("td", {}, ago(r.saved_at), r.saved_by ? ` · ${r.saved_by}` : ""),
          h("td", { class: "muted" }, r.note === "before reset" ? "replaced by Reset" : "replaced by an edit", ` · ${r.chars.toLocaleString()} chars`),
          h("td", {}, h("button", { class: "link", onclick: (e) => busy(e.target, async () => {
            const old = await get(`/api/prompts/${pid}/revisions/${r.id}`);
            text.value = old.body;
            if (tier && old.tier) tier.value = old.tier;
            refresh();
            toast("Loaded into the editor. Save to use it.");
            text.focus();
          }) }, "Load into editor"))))))));
    }
  };

  const save = (button) => busy(button, async () => {
    d = await put(`/api/prompts/${pid}`, { body: text.value, ...(tier ? { tier: tier.value } : {}) });
    saved = d.body;
    text.value = d.body;
    drawMeta(); refresh();
    toast(d.customized ? "Saved. The assistant uses it from the next message." : "Saved (same as the original, so the original is used).");
  });
  const saveBtn = h("button", { class: "primary", onclick: (e) => save(e.target) }, "Save");
  text.addEventListener("input", refresh);
  tier?.addEventListener("change", refresh);
  text.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(saveBtn); } });

  const insert = (name) => {
    const token = `{${name}}`, at = text.selectionStart ?? text.value.length;
    text.setRangeText(token, at, text.selectionEnd ?? at, "end");
    text.focus(); refresh();
  };

  el.append(h("div", {},
    h("div", { class: "row small" }, link("#/prompts", "← All prompts"), h("span", { class: "muted" }, `· ${d.group}`)),
    h("div", { class: "between" }, h("h1", {}, d.title), tier ? h("label", { class: "row" }, "Runs on", tier) : null),
    h("p", { class: "muted" }, d.description),
    d.placeholders.length ? h("div", { class: "row small", style: { marginBottom: "6px" } }, h("span", { class: "muted" }, "Insert:"),
      ...d.placeholders.map((p) => h("button", { class: "chip", title: p.meaning, onclick: () => insert(p.name) }, `{${p.name}}`))) : null,
    text,
    h("div", { class: "row", style: { marginTop: "8px" } }, count, state, h("span", { class: "spacer" }),
      h("button", { onclick: () => { text.value = saved; if (tier) tier.value = d.tier; refresh(); } }, "Discard changes"),
      h("button", { class: "danger", onclick: (e) => busy(e.target, async () => {
        if (!confirm("Go back to the original text? Your version stays under Earlier versions.")) return;
        d = await del(`/api/prompts/${pid}`);
        saved = d.body; text.value = d.body; if (tier) tier.value = d.tier;
        drawMeta(); refresh(); toast("Back to the original.");
      }) }, "Reset to original"),
      saveBtn),
    meta, warnings,
    d.tools.length ? h("details", { class: "card" }, h("summary", {}, `Tools this task can use (${d.tools.length})`),
      h("p", { class: "small muted" }, "Set in the app's code, not here: which tools a task has decides what it can change ",
        "(only Quick Shop can add to the cart, and Notion edits always ask first). Mentioning a tool in the text doesn't grant it."),
      h("p", { class: "small" }, d.tools.join(", "))) : null,
    h("details", { class: "card" }, h("summary", {}, "Original text"), h("pre", { class: "mono-pre" }, d.default || "(empty)")),
    d.id.startsWith("tasks/") || d.id === "jobs/weekly_plan" ? h("details", { class: "card", ontoggle: async (e) => {
      if (!e.target.open || previewBox.firstChild) return;
      const p = await get(`/api/prompts/${pid}/preview`);
      previewBox.append(h("p", { class: "small muted" }, `What the model receives (saved versions, placeholders filled): `,
        `${p.chars.toLocaleString()} characters, about ${p.approx_tokens.toLocaleString()} tokens, sent with every step of a reply.`),
        h("pre", { class: "mono-pre" }, p.text));
    } }, h("summary", {}, "Preview the full prompt"), previewBox) : null,
    history));
  drawMeta(); refresh();

  const leave = (e) => { if (dirty()) { e.preventDefault(); e.returnValue = ""; } };
  window.addEventListener("beforeunload", leave);
  return { dirty, cleanup: () => window.removeEventListener("beforeunload", leave) };
}
