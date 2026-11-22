// The web app: a hash router over the views. Each view module exports render(el, params, query).
import { get } from "./lib/api.js";
import { clear, errorBox, h } from "./lib/ui.js";

const ROUTES = [
  [/^#\/?$/, "home", "Home"],
  [/^#\/quick$/, "quick", "Quick Shop"],
  [/^#\/runs\/([\w-]+)$/, "run"],
  [/^#\/runs$/, "runs", "Runs"],
  [/^#\/meals$/, "meals", "Meals"],
  [/^#\/deals$/, "deals", "Deals"],
  [/^#\/cart$/, "cart", "Cart"],
  [/^#\/product\/([\w-]+)$/, "product"],
  [/^#\/orders\/(\d+)$/, "order"],
  [/^#\/orders$/, "orders", "Orders"],
  [/^#\/history$/, "history", "History"],
  [/^#\/chat\/([\w-]+)$/, "chat"],
  [/^#\/chat$/, "chat", "Chat"],
  [/^#\/settings$/, "settings", "Settings"],
];

const NAV = ROUTES.filter((r) => r[2]);
const view = document.getElementById("view");
const nav = document.getElementById("nav");
let current = null;

function renderNav(name) {
  clear(nav);
  for (const [, mod, label] of NAV) {
    const hash = mod === "home" ? "#/" : `#/${mod}`;
    const on = name === mod || (name === "run" && mod === "runs") || (name === "order" && mod === "orders") || (name === "product" && mod === "cart");
    nav.append(h("a", { href: hash, class: on ? "on" : "" }, label));
  }
}

async function route() {
  const [path, qs] = (location.hash || "#/").split("?");
  const query = Object.fromEntries(new URLSearchParams(qs || ""));
  const match = ROUTES.map(([re, mod]) => [re.exec(path), mod]).find(([m]) => m);
  const [m, mod] = match || [[], "home"];
  renderNav(mod);
  if (current?.cleanup) current.cleanup();
  clear(view);
  try {
    const module = await import(`./views/${mod}.js`);
    current = (await module.render(view, m.slice(1), query)) || null;
  } catch (e) {
    view.append(errorBox(e));
    console.error(e);
  }
  window.scrollTo(0, 0);
}

async function sessionPill() {
  const pill = document.getElementById("session");
  try {
    const s = await get("/api/retailer/session");
    pill.className = `pill ${s.state}`;
    pill.textContent = s.state === "connected" ? "No Frills ✓" : "No Frills: sign in";
    pill.title = s.state === "connected" ? "Signed in to No Frills; the session renews itself." : "Sign in to No Frills in Settings.";
  } catch {
    pill.className = "pill required";
    pill.textContent = "Offline";
  }
}

window.addEventListener("hashchange", route);
window.addEventListener("vibe:changed", sessionPill);
route();
sessionPill();
setInterval(sessionPill, 5 * 60_000);
window.addEventListener("focus", sessionPill);
