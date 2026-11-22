// Quick Shop: say what you need and the assistant puts it straight into the cart (no review step).
// Everything it adds is recorded as a run, so any item can be taken out again from the run or the cart page.
import { changed, post, stream } from "../lib/api.js";
import { clear, h, link, richText } from "../lib/ui.js";

const LABELS = {
  shopping_search_many: "Finding the products", shopping_get_cart: "Checking what's already in the cart",
  shopping_get_context: "Checking deals and your usual picks", shopping_get_preferences: "Checking preferences",
  notion_pantry: "Checking the pantry", shopping_quick_add: "Adding to the cart",
};

export function quickBox({ compact = false } = {}) {
  const input = h("textarea", { rows: compact ? 2 : 4, placeholder: "e.g. 2 L of 2% milk, a dozen eggs, bananas and stuff for tacos" });
  const go = h("button", { class: "primary" }, "Add to cart");
  const stop = h("button", { hidden: true }, "Stop");
  const out = h("div");
  let controller = null;

  async function run() {
    const text = input.value.trim();
    if (!text || controller) return;
    controller = new AbortController();
    go.disabled = true; stop.hidden = false;
    clear(out);
    const steps = h("div");
    const status = h("div", { class: "toolline" }, "Working out what you need…");
    out.append(h("div", { class: "card" }, steps, status));
    let reply = "", bubble = null, threadId = null, added = false;
    try {
      ({ thread_id: threadId } = await post("/api/chat/threads", { mode: "quick" }));
      for await (const ev of stream(`/api/chat/threads/${threadId}/messages`, { text, mode: "quick" }, controller.signal)) {
        if (ev.type === "tool" && ev.status === "running") {
          steps.append(h("div", { class: "toolline" }, "⚙ ", LABELS[ev.name] || ev.name, "…"));
        } else if (ev.type === "tool" && ev.status === "done" && ev.name === "shopping_quick_add") {
          added = true;
        } else if (ev.type === "tool" && ev.status === "error") {
          steps.append(h("div", { class: "toolline error" }, "✕ ", (ev.summary || "").slice(0, 200)));
        } else if (ev.type === "text") {
          reply += ev.text;
          if (!bubble) { bubble = h("div"); steps.after(bubble); }
          bubble.textContent = reply;
        } else if (ev.type === "notice") {
          steps.append(h("div", { class: "toolline error" }, ev.text));
        } else if (ev.type === "error") {
          steps.append(h("div", { class: "flash err" }, ev.message));
        }
      }
      if (bubble) bubble.replaceWith(richText(reply));
      if (added) input.value = "";
    } catch (e) {
      if (e.name !== "AbortError") out.append(h("div", { class: "flash err" }, e.message));
    } finally {
      status.remove();
      go.disabled = false; stop.hidden = true; controller = null;
      if (threadId) {
        out.firstChild?.append(h("div", { class: "row small", style: { marginTop: "8px" } },
          link("#/cart", "Open the cart"), "·", link(`#/chat/${threadId}`, "Continue in chat (e.g. “swap the eggs for large”)")));
      }
      changed();
    }
  }

  go.addEventListener("click", run);
  stop.addEventListener("click", () => controller?.abort());
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
  return h("div", {},
    input,
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("span", { class: "small muted" }, "Goes straight into the cart. You still check out yourself on nofrills.ca."),
      h("span", { class: "spacer" }), stop, go),
    out);
}

export async function render(el) {
  el.append(h("h1", {}, "⚡ Quick Shop"),
    h("p", { class: "muted" }, "Type what you need. The assistant picks good-value products and adds them to your No Frills cart right away. ",
      "Every Quick Shop is saved under ", link("#/runs", "Runs"), "; change or remove items on the ", link("#/cart", "Cart"), " page."),
    h("div", { class: "card" }, quickBox()));
}
