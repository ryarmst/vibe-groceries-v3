// Quick Shop: say what you need and the assistant puts it straight into the cart (no review step).
// Everything it adds is recorded as a run, so items can be changed or removed on the Cart page.
// The work happens on the server: leaving the page doesn't stop it, and coming back shows it again.
import { changed, get, post, reply } from "../lib/api.js";
import { toolLabel } from "../lib/tools.js";
import { clear, h, link, richText } from "../lib/ui.js";

const KEY = "vibe.quickThread"; // the latest Quick Shop, so coming back to the page can pick it up

function remember(id) { try { sessionStorage.setItem(KEY, id); } catch { /* private mode */ } }
function recall() { try { return sessionStorage.getItem(KEY); } catch { return null; } }

export function quickBox({ compact = false } = {}) {
  const input = h("textarea", { rows: compact ? 2 : 4, "aria-label": "What do you need?",
    placeholder: "e.g. 2 L of 2% milk, a dozen eggs, bananas and stuff for tacos" });
  const go = h("button", { class: "primary" }, "Add to cart");
  const stop = h("button", { hidden: true }, "Stop");
  const out = h("div", { "aria-live": "polite" });
  let controller = null, threadId = null;

  async function show(start) {
    controller?.abort();
    const mine = controller = new AbortController();
    go.disabled = true; stop.hidden = false;
    const status = h("div", { class: "toolline working-line" }, "Working out what you need…");
    const problems = h("div");
    const result = h("div");
    clear(out).append(h("div", { class: "card" }, status, problems, result));
    let text = "", added = false;
    try {
      for await (const ev of reply(threadId, { start, signal: mine.signal })) {
        if (ev.type === "tool" && ev.status === "running") status.textContent = `${toolLabel(ev.name)}…`;
        else if (ev.type === "tool" && ev.status === "done" && ev.name === "shopping_quick_add") added = true;
        else if (ev.type === "tool" && ev.status === "error") problems.append(h("div", { class: "toolline error" }, "✕ ", (ev.summary || "").slice(0, 200)));
        else if (ev.type === "reconnecting") status.textContent = "Reconnecting…";
        else if (ev.type === "text") { text += ev.text; result.textContent = text; }
        else if (ev.type === "notice") problems.append(h("div", { class: "toolline error" }, ev.text));
        else if (ev.type === "error") problems.append(h("div", { class: "flash err" }, ev.message));
      }
      result.replaceChildren(richText(text));
      if (added) input.value = "";
    } catch (e) {
      if (e.name !== "AbortError") problems.append(h("div", { class: "flash err" }, e.message));
    } finally {
      status.remove();
      if (controller === mine) {
        go.disabled = false; stop.hidden = true; controller = null;
        out.firstChild?.append(h("div", { class: "row small", style: { marginTop: "8px" } },
          link("#/cart", "Open the cart"), h("span", { class: "muted" }, "·"), link(`#/chat/${threadId}`, "Change something in Chat")));
      }
      changed();
    }
  }

  async function run() {
    const text = input.value.trim();
    if (!text || controller) return;
    go.disabled = true;
    try {
      ({ thread_id: threadId } = await post("/api/chat/threads", { mode: "quick" }));
    } catch (e) {
      clear(out).append(h("div", { class: "flash err" }, e.message)); go.disabled = false; return;
    }
    remember(threadId);
    await show({ path: `/api/chat/threads/${threadId}/messages`, body: { text, mode: "quick" } });
  }

  go.addEventListener("click", run);
  stop.addEventListener("click", () => threadId && post(`/api/chat/threads/${threadId}/stop`).catch(() => {}));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });

  // Still working on one from before (page changed, phone locked)? Show it.
  const previous = recall();
  if (previous) {
    get(`/api/chat/threads/${previous}`).then((t) => { if (t.working) { threadId = previous; show(null); } }).catch(() => {});
  }

  const box = h("div", {},
    input,
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("span", { class: "small muted grow" }, "Goes straight into the cart. You check out on nofrills.ca."), stop, go),
    out);
  box.cleanup = () => controller?.abort(); // stop watching; the server finishes the job
  return box;
}

export async function render(el) {
  const box = quickBox();
  el.append(h("h1", {}, "⚡ Quick Shop"),
    h("p", { class: "muted" }, "Type what you need. The assistant picks good-value products and adds them to your cart. ",
      "You can change or remove anything on the ", link("#/cart", "Cart"), " page."),
    h("div", { class: "card" }, box));
  return { cleanup: box.cleanup };
}
