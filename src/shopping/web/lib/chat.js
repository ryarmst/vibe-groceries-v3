// Chat with the assistant: threads, a task picker, streamed replies, tool activity, confirmation cards.
import { changed, del, get, post, stream } from "./api.js";
import { toolLabel } from "./tools.js";
import { busy, clear, h, richText, toast } from "./ui.js";

// Each task gives the assistant focused instructions and only the tools for that job (more reliable, cheaper).
const TASKS = [
  { mode: "quick", icon: "⚡", title: "Quick Shop", hint: "Say what you need; it goes straight into the cart (no review)",
    placeholder: "e.g. “2 L 2% milk, a dozen eggs and stuff for tacos”" },
  { mode: "shop", icon: "🛒", title: "Plan a shop to review", hint: "A run from your list, regulars and this week's deals, for you to approve",
    placeholder: "Anything specific? e.g. “we also need bread and soda”" },
  { mode: "meals", icon: "🍽️", title: "Plan meals", hint: "Meals around the deals and what needs using up",
    placeholder: "e.g. “plan this week's dinners” or “4 dinners, one vegetarian”" },
  { mode: "pantry", icon: "🥫", title: "Update the pantry or list", hint: "Paste a receipt, used something up, add to the grocery list",
    placeholder: "e.g. “we used up the eggs” or paste a receipt" },
  { mode: "recipes", icon: "📖", title: "Find, cook or save a recipe", hint: "What can I make with…, look up or save to Cooking",
    placeholder: "e.g. “what can I make with leeks and potatoes?”" },
  { mode: "ask", icon: "❓", title: "Ask a question", hint: "Prices, deals, past orders, what's in the pantry",
    placeholder: "e.g. “is $4.99 good for 18 eggs?”" },
];
const AUTO = { mode: "auto", icon: "✨", title: "Chosen automatically", placeholder: "Ask, shop, plan meals, or paste a receipt…" };
const taskFor = (mode) => TASKS.find((t) => t.mode === mode) || AUTO;
export function mountChat(root, { threadId = null, onThread = () => {}, mode: startMode = "auto" } = {}) {
  let thread = threadId, mode = startMode, controller = null, picking = false;
  const log = h("div", { class: "log" });
  const input = h("textarea", { rows: 2, placeholder: "Ask, shop, plan meals, or paste a receipt…  (Enter to send, Shift+Enter for a new line)" });
  const sendBtn = h("button", { class: "primary" }, "Send");
  const stopBtn = h("button", { hidden: true, onclick: () => controller?.abort() }, "Stop");
  const taskBar = h("div", { class: "row small" });
  const threadSelect = h("select", { style: { maxWidth: "60%" } });

  function choose(m) {
    mode = m;
    picking = false;
    drawTask();
    const empty = log.querySelector(".task-picker");
    if (empty) empty.remove();
    input.focus();
  }

  function picker(compact = false) {
    return h("div", { class: `task-picker ${compact ? "compact" : ""}` },
      h("h3", { class: "picker-title" }, "What would you like to do?"),
      h("div", { class: "task-grid" }, ...TASKS.map((t) => h("button", { class: `task ${t.mode === mode ? "on" : ""}`, onclick: () => choose(t.mode) },
        h("span", { class: "task-icon" }, t.icon), h("span", {}, h("strong", {}, t.title), h("span", { class: "small muted block" }, t.hint))))),
      h("p", { class: "small muted" }, "Or just type below and the assistant will work out the task."));
  }

  function drawTask() {
    const t = taskFor(mode);
    input.placeholder = t.placeholder + "  (Enter to send, Shift+Enter for a new line)";
    clear(taskBar).append(h("span", { class: "muted" }, "Task:"), h("strong", {}, `${t.icon} ${t.title}`),
      h("button", { class: "link", onclick: () => {
        picking = !picking;
        const existing = taskBar.parentElement.querySelector(".task-picker");
        if (existing) existing.remove();
        if (picking) taskBar.after(picker(true));
      } }, picking ? "Close" : "Change"));
  }

  async function loadThreads() {
    const { threads } = await get("/api/chat/threads");
    clear(threadSelect).append(h("option", { value: "" }, "New conversation"),
      ...threads.map((t) => h("option", { value: t.thread_id, selected: t.thread_id === thread }, (t.system ? "📅 " : "") + t.title)));
  }

  async function open(id) {
    thread = id || null;
    onThread(thread);
    clear(log);
    if (!thread) { mode = startMode; drawTask(); if (mode === "auto") log.append(picker()); else input.focus(); return; }
    const t = await get(`/api/chat/threads/${thread}`);
    mode = t.mode || "auto";
    drawTask();
    for (const m of t.messages) renderStored(m);
    log.scrollTop = log.scrollHeight;
  }

  function renderStored(m) {
    if (m.role === "user") log.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content)));
    else if (m.role === "assistant") {
      if (m.content) log.append(h("div", { class: "msg assistant" }, h("div", { class: "bubble" }, richText(m.content)),
        m.model ? h("div", { class: "meta" }, `${m.provider} · ${m.model}${m.cost_usd ? ` · $${m.cost_usd.toFixed(4)}` : ""}`) : null));
      for (const c of m.tool_calls || []) log.append(h("div", { class: "toolline" }, "⚙ ", toolLabel(c.name)));
    } else if (m.role === "tool" && m.status === "pending_confirmation") log.append(confirmCard({ message_id: m.id, name: m.tool_name, preview: m.preview }));
    else if (m.role === "tool" && m.status === "declined") log.append(h("div", { class: "toolline error" }, "✕ ", m.content));
  }

  function confirmCard(ev) {
    const note = h("input", { placeholder: "Note if declining (optional)", style: { flex: 1 } });
    const card = h("div", { class: "confirm" }, h("strong", {}, "Confirm this change?"), h("pre", {}, ev.preview || ev.name),
      h("div", { class: "row" }, note,
        h("button", { onclick: (e) => busy(e.target, () => resolve(card, ev.message_id, false, note.value)) }, "Decline"),
        h("button", { class: "primary", onclick: (e) => busy(e.target, () => resolve(card, ev.message_id, true, null)) }, "Approve")));
    return card;
  }

  async function resolve(card, messageId, approve, note) {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    card.append(h("div", { class: "small muted" }, approve ? "Approved." : "Declined."));
    await run(`/api/chat/threads/${thread}/confirm/${messageId}`, { approve, note: note || null });
  }

  async function run(path, body) {
    controller = new AbortController();
    sendBtn.disabled = true; stopBtn.hidden = false;
    let bubble = null, text = "", meta = null;
    const status = h("div", { class: "toolline" }, "Thinking…");
    log.append(status);
    const scroll = () => { log.scrollTop = log.scrollHeight; };
    try {
      for await (const ev of stream(path, body, controller.signal)) {
        if (ev.type === "mode") status.textContent = `${ev.title} mode…`;
        else if (ev.type === "text") {
          if (!bubble) { bubble = h("div", { class: "bubble" }); log.append(h("div", { class: "msg assistant" }, bubble)); }
          text += ev.text;
          bubble.textContent = text;
        } else if (ev.type === "tool") {
          if (ev.status === "running") log.append(h("div", { class: "toolline" }, "⚙ ", toolLabel(ev.name), "…"));
          else if (ev.status === "error" || ev.status === "declined") log.append(h("div", { class: "toolline error" }, "✕ ", toolLabel(ev.name), ": ", (ev.summary || "").slice(0, 200)));
          finish();
        } else if (ev.type === "confirm") { finish(); log.append(confirmCard(ev)); }
        else if (ev.type === "notice") log.append(h("div", { class: "toolline error" }, ev.text));
        else if (ev.type === "usage") meta = `${ev.provider} · ${ev.model}${ev.cost_usd ? ` · $${ev.cost_usd.toFixed(4)}` : ""}`;
        else if (ev.type === "error") log.append(h("div", { class: "flash err" }, ev.message));
        scroll();
      }
    } catch (e) {
      if (e.name !== "AbortError") log.append(h("div", { class: "flash err" }, e.message));
    } finally {
      finish();
      status.remove();
      sendBtn.disabled = false; stopBtn.hidden = true; controller = null;
      changed();
      scroll();
    }
    function finish() {
      if (bubble) { const done = richText(text); bubble.replaceWith(done); if (meta) done.after(h("div", { class: "meta" }, meta)); }
      bubble = null; text = ""; meta = null;
    }
  }

  async function send() {
    const textValue = input.value.trim();
    if (!textValue || controller) return;
    if (!thread) {
      const t = await post("/api/chat/threads", { mode });
      thread = t.thread_id;
      onThread(thread);
      clear(log);
    }
    input.value = "";
    log.querySelector(".task-picker")?.remove();
    root.querySelector(".composer .task-picker")?.remove();
    picking = false;
    drawTask();
    log.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, textValue)));
    await run(`/api/chat/threads/${thread}/messages`, { text: textValue, mode });
    loadThreads();
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  threadSelect.addEventListener("change", () => open(threadSelect.value));

  root.append(h("div", { class: "chat" },
    h("div", { class: "row", style: { padding: "8px 10px", borderBottom: "1px solid var(--line)" } }, threadSelect,
      h("button", { class: "link", onclick: () => { threadSelect.value = ""; open(null); } }, "New"),
      h("span", { class: "spacer" }),
      h("button", { class: "link", title: "Delete this conversation", onclick: async () => {
        if (!thread || !confirm("Delete this conversation?")) return;
        await del(`/api/chat/threads/${thread}`); toast("Deleted."); await loadThreads(); open(null);
      } }, "🗑")),
    log,
    h("div", { class: "composer" }, taskBar, h("div", { style: { marginTop: "6px" } }, input),
      h("div", { class: "row", style: { marginTop: "6px" } }, h("span", { class: "small muted" }, "Changes to Notion ask first. Only Quick Shop adds to the cart directly."),
        h("span", { class: "spacer" }), stopBtn, sendBtn))));
  drawTask();
  loadThreads().then(() => open(thread)).catch((e) => log.append(h("div", { class: "flash err" }, e.message)));
  return { open };
}
