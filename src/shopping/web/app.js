// The web app: a hash router over the views. Each view module exports render(el, params, query) and may
// return { cleanup, dirty } (cleanup when leaving; dirty() to ask before leaving with unsaved changes).
import { get } from "./lib/api.js";
import { clear, errorBox, h } from "./lib/ui.js";

const ROUTES = [
  [/^#\/?$/, "home"],
  [/^#\/quick$/, "quick"],
  [/^#\/runs\/([\w-]+)$/, "run"],
  [/^#\/runs$/, "runs"],
  [/^#\/meals$/, "meals"],
  [/^#\/deals$/, "deals"],
  [/^#\/cart$/, "cart"],
  [/^#\/product\/([\w-]+)$/, "product"],
  [/^#\/orders\/(\d+)$/, "order"],
  [/^#\/orders$/, "orders"],
  [/^#\/history$/, "history"],
  [/^#\/chat\/([\w-]+)$/, "chat"],
  [/^#\/chat$/, "chat"],
  [/^#\/prompts\/(.+)$/, "prompts"],
  [/^#\/prompts$/, "prompts"],
  [/^#\/settings$/, "settings"],
];

// The menu: everyday pages first; the rest under "More" (on a phone, everything is in one menu).
const MAIN = [["home", "Home"], ["quick", "Quick Shop"], ["runs", "Runs"], ["deals", "Deals"], ["cart", "Cart"], ["chat", "Chat"]];
const EXTRA = [["meals", "Meals"], ["orders", "Orders"], ["history", "History"], ["prompts", "Prompts"], ["settings", "Settings"]];
const SECTION = { run: "runs", product: "cart", order: "orders" }; // detail pages highlight their list

const view = document.getElementById("view");
const nav = document.getElementById("nav");
const menuBtn = document.getElementById("menu");
let current = null, shownHash = location.hash, seq = 0;

const href = (mod) => (mod === "home" ? "#/" : `#/${mod}`);

function renderNav(mod) {
  const active = SECTION[mod] || mod;
  const item = ([m, label]) => h("a", { href: href(m), class: m === active ? "on" : "", "aria-current": m === active ? "page" : null }, label);
  const extraOn = EXTRA.some(([m]) => m === active);
  clear(nav).append(...MAIN.map(item),
    h("details", { class: `nav-more ${extraOn ? "on" : ""}` }, h("summary", {}, "More"), h("div", { class: "nav-menu" }, ...EXTRA.map(item))),
    h("div", { class: "nav-extra" }, ...EXTRA.map(item))); // the phone menu lists everything
}

function closeMenus() {
  document.body.classList.remove("menu-open");
  menuBtn.setAttribute("aria-expanded", "false");
  nav.querySelectorAll("details[open]").forEach((d) => d.removeAttribute("open"));
}

menuBtn.addEventListener("click", () => {
  const open = document.body.classList.toggle("menu-open");
  menuBtn.setAttribute("aria-expanded", String(open));
});
document.addEventListener("click", (e) => { if (!e.target.closest("header")) closeMenus(); });

async function route() {
  // A view with unsaved edits (the prompt editor) can ask before it's replaced.
  if (location.hash !== shownHash && current?.dirty?.() && !confirm("Leave without saving your changes?")) {
    history.replaceState(null, "", shownHash || "#/");
    return;
  }
  shownHash = location.hash;
  closeMenus();
  const mine = ++seq;
  const [path, qs] = (location.hash || "#/").split("?");
  const query = Object.fromEntries(new URLSearchParams(qs || ""));
  const match = ROUTES.map(([re, mod]) => [re.exec(path), mod]).find(([m]) => m);
  const [m, mod] = match || [[], "home"];
  renderNav(mod);
  if (current?.cleanup) current.cleanup();
  current = null;
  // Render into a fresh page; only show it if the person hasn't moved on meanwhile (slow loads can't mix pages).
  const page = h("div", { class: "page" });
  const spinner = setTimeout(() => { if (mine === seq) view.replaceChildren(h("div", { class: "loading", role: "status" }, "Loading…")); }, 250);
  let result = null;
  try {
    const module = await import(`./views/${mod}.js`);
    result = (await module.render(page, m.slice(1), query)) || null;
  } catch (e) {
    page.replaceChildren(errorBox(e));
    console.error(e);
  } finally {
    clearTimeout(spinner);
  }
  if (mine !== seq) { result?.cleanup?.(); return; }
  current = result;
  view.replaceChildren(page);
  window.scrollTo(0, 0);
  page.querySelector("h1")?.setAttribute("tabindex", "-1");
}

async function sessionPill() {
  const pill = document.getElementById("session");
  try {
    const s = await get("/api/retailer/session");
    pill.className = `pill ${s.state}`;
    pill.textContent = s.state === "connected" ? "No Frills ✓" : "No Frills: sign in";
    pill.title = s.state === "connected" ? "Signed in to No Frills; the sign-in renews itself." : "Sign in to No Frills in Settings.";
  } catch {
    pill.className = "pill required";
    pill.textContent = "Offline";
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("vibe:changed", sessionPill);
window.addEventListener("focus", sessionPill);
route();
sessionPill();
setInterval(() => { if (!document.hidden) sessionPill(); }, 5 * 60_000);
