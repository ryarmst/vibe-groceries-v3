// DOM helpers. Everything from the server is inserted as text (never innerHTML), so product names,
// Notion content or model output can't inject markup.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (/^(inner|outer)HTML$|^srcdoc$/i.test(k)) throw new Error(`refusing to set ${k}`);
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k in el && !["list", "form", "type"].includes(k)) el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.firstChild.remove(); return el; }

export const money = (v) => (v === null || v === undefined ? "—" : `$${Number(v).toFixed(2)}`);
export const pct = (v) => (v ? `${Math.round(v)}% off` : "");

export function unit(price, basis) {
  if (price === null || price === undefined || !basis) return "";
  const label = { "100g": "100 g", "100ml": "100 mL", "1ea": "item" }[basis] || basis;
  return price >= 0.1 || basis === "1ea" ? `$${Number(price).toFixed(2)} / ${label}` : `${(price * 100).toFixed(1)}¢ / ${label}`;
}

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function day(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

let toastBox;
export function toast(message, kind = "ok") {
  if (!message) return;
  toastBox ||= document.body.appendChild(h("div", { class: "toasts", "aria-live": "polite" }));
  const t = h("div", { class: `toast ${kind}`, onclick: () => t.remove() }, message);
  toastBox.append(t);
  setTimeout(() => t.remove(), kind === "err" ? 10000 : 5000);
}

// Run an async action from a button: disables it while running and reports errors.
export async function busy(button, fn) {
  const label = button?.textContent;
  if (button) { button.disabled = true; button.dataset.busy = "1"; }
  try { return await fn(); }
  catch (e) { toast(e.message || String(e), "err"); throw e; }
  finally { if (button) { button.disabled = false; delete button.dataset.busy; if (label) button.textContent = label; } }
}

// Internal links: "#/runs/abc".
export function openApp(hash = "#/") { location.hash = hash; }

// External links from data (Notion page URLs, product links): https only, opened without opener access.
export function extLink(url, ...children) {
  let ok = false;
  try { ok = new URL(url).protocol === "https:"; } catch { /* not a URL */ }
  return ok ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", class: "small" }, ...children) : null;
}

export function link(hash, ...children) {
  return h("a", { href: hash }, ...children);
}

// Render model/assistant text: paragraphs, bullets, **bold**, and #/ links. Text only, no HTML.
export function richText(text) {
  const box = h("div", { class: "rich" });
  let list = null;
  for (const raw of String(text || "").split("\n")) {
    const line = raw.trimEnd();
    const bullet = /^\s*([-*•]|\d+[.)])\s+/.exec(line);
    if (bullet) {
      if (!list) { list = h(/\d/.test(bullet[1]) ? "ol" : "ul"); box.append(list); }
      list.append(h("li", {}, inline(line.slice(bullet[0].length))));
      continue;
    }
    list = null;
    if (!line.trim()) continue;
    const heading = /^#{1,3}\s+(.*)$/.exec(line);
    box.append(heading ? h("h4", {}, inline(heading[1])) : h("p", {}, inline(line)));
  }
  return box;
}

function inline(text) {
  const out = [];
  const re = /\*\*(.+?)\*\*|(#\/[\w\-/?=&.]+)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    out.push(m[1] ? h("strong", {}, m[1]) : link(m[2], m[2].startsWith("#/runs/") ? "review run" : m[2].slice(2)));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function badge(text, kind = "") { return h("span", { class: `badge ${kind}` }, text); }

export function empty(text) { return h("div", { class: "empty" }, text); }

export function errorBox(e) {
  const box = h("div", { class: "flash err" }, e.message || String(e));
  if (e.code === "access_expired") box.append(" ", h("button", { class: "link", onclick: () => location.reload() }, "Reload"));
  if (e.code === "needs_session" && !/Settings/.test(e.message)) box.append(" ", link("#/settings", "Sign in to No Frills in Settings"));
  return box;
}
