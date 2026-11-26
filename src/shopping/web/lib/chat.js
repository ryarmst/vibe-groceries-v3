// Chat with the assistant: conversations, a task picker, live replies, tool activity, confirmation cards.
// Replies run on the server: leaving the page or losing the connection doesn't stop them, and reopening a
// conversation that's still working picks the reply up again.
import { changed, del, get, post, reply, startReply } from "./api.js";
import { toolLabel } from "./tools.js";
import { busy, clear, h, richText, toast } from "./ui.js";

// Each task gives the assistant focused instructions and only the tools for that job (more reliable, cheaper).
export const TASKS = [
  { mode: "quick", icon: "⚡", title: "Quick Shop", hint: "Say what you need; it goes straight into the cart",
    placeholder: "e.g. “2 L 2% milk, a dozen eggs and stuff for tacos”" },
  { mode: "week", icon: "🗓️", title: "Plan my week", hint: "Healthy batch meals around the deals, plus the full shopping run",
    placeholder: "Anything to add? e.g. “one vegetarian meal” (or just Send)" },
  { mode: "shop", icon: "🛒", title: "Plan a shop to review", hint: "From your list, regulars and this week's deals",
    placeholder: "Anything specific? e.g. “we also need bread and soda”" },
  { mode: "meals", icon: "🍽️", title: "Plan meals", hint: "Around the deals and what needs using up",
    placeholder: "e.g. “plan this week's dinners” or “4 dinners, one vegetarian”" },
  { mode: "pantry", icon: "🥫", title: "Pantry & grocery list", hint: "Paste a receipt, used something up, add to the list",
    placeholder: "e.g. “we used up the eggs” or paste a receipt" },
  { mode: "recipes", icon: "📖", title: "Recipes", hint: "What can I make with…, or save one to Cooking",
    placeholder: "e.g. “what can I make with leeks and potatoes?”" },
  { mode: "ask", icon: "❓", title: "Ask a question", hint: "Prices, deals, past orders, the pantry",
    placeholder: "e.g. “is $4.99 good for 18 eggs?”" },
];
const AUTO = { mode: "auto", icon: "✨", title: "Any", placeholder: "Ask, shop, plan meals, or paste a receipt…" };
const taskFor = (mode) => TASKS.find((t) => t.mode === mode) || AUTO;
const KEYS_HINT = matchMedia("(pointer: fine)").matches ? "  (Enter to send, Shift+Enter for a new line)" : "";

const WEEK_REQUEST = "Plan my week: healthy batch meals around this week's deals, and the full shopping run.";

// The one-tap weekly plan: start it on the server, then open the conversation to watch it work.
export async function planMyWeek() {
  const { thread_id: id } = await post("/api/chat/threads", { mode: "week" });
  await startReply(id, { text: WEEK_REQUEST, mode: "week" });
  location.hash = `#/chat/${id}?follow=1`;
}

export function mountChat(root, { threadId = null, onThread = () => {}, mode: startMode = "auto", replay = false } = {}) {
  let thread = threadId, mode = startMode, controller = null;
  const log = h("div", { class: "log", "aria-live": "polite" });
  const input = h("textarea", { rows: 2 });
  const sendBtn = h("button", { class: "primary" }, "Send");
  const stopBtn = h("button", { hidden: true, onclick: () => thread && post(`/api/chat/threads/${thread}/stop`).catch(() => {}) }, "Stop");
  const taskSelect = h("select", { "aria-label": "Task" },
    h("option", { value: "auto" }, "✨ Any task"), ...TASKS.map((t) => h("option", { value: t.mode }, `${t.icon} ${t.title}`)));
  const threadSelect = h("select", { "aria-label": "Conversation", class: "grow" });

  const setMode = (m) => { mode = m; taskSelect.value = m; input.placeholder = taskFor(m).placeholder + KEYS_HINT; };
  taskSelect.addEventListener("change", () => setMode(taskSelect.value));

  function picker() {
    return h("div", { class: "task-picker" },
      h("h3", { class: "picker-title" }, "What would you like to do?"),
      h("div", { class: "task-grid" }, ...TASKS.map((t) => h("button", { class: "task", onclick: () => { setMode(t.mode); log.querySelector(".task-picker")?.remove(); input.focus(); } },
        h("span", { class: "task-icon" }, t.icon), h("span", {}, h("strong", {}, t.title), h("span", { class: "small muted block" }, t.hint))))),
      h("p", { class: "small muted" }, "Or just type below, and the assistant works out the task."));
  }

  async function loadThreads() {
    const { threads } = await get("/api/chat/threads");
    clear(threadSelect).append(h("option", { value: "" }, "New conversation"),
      ...threads.map((t) => h("option", { value: t.thread_id, selected: t.thread_id === thread }, (t.system ? "📅 " : "") + t.title)));
  }

  async function open(id) {
    controller?.abort(); // stop watching the previous reply (it carries on on the server)
    thread = id || null;
    onThread(thread);
    clear(log);
    if (!thread) { setMode(startMode); if (startMode === "auto") log.append(picker()); input.focus(); return; }
    const t = await get(`/api/chat/threads/${thread}`);
    setMode(t.mode || "auto");
    if (replay) {
      // Just started from a button: show the request, then replay the reply's progress from the beginning
      // (including a quick failure that ended before this page opened).
      replay = false;
      for (const m of t.messages.filter((x) => x.role === "user")) renderStored(m);
      follow();
      return;
    }
    for (const m of t.messages) renderStored(m);
    log.scrollTop = log.scrollHeight;
    if (t.working) follow(); // a reply still in progress: show it live
  }

  function renderStored(m) {
    if (m.role === "user") log.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, m.content)));
    else if (m.role === "assistant") {
      for (const c of m.tool_calls || []) log.append(h("div", { class: "toolline" }, "✓ ", toolLabel(c.name)));
      if (m.content) log.append(h("div", { class: "msg assistant" }, h("div", { class: "bubble" }, richText(m.content))));
    } else if (m.role === "tool" && m.status === "pending_confirmation") log.append(confirmCard({ message_id: m.id, name: m.tool_name, preview: m.preview }));
    else if (m.role === "tool" && m.status === "declined") log.append(h("div", { class: "toolline error" }, "✕ ", m.content));
  }

  function confirmCard(ev) {
    const note = h("input", { placeholder: "Why not? (optional)", class: "grow" });
    const card = h("div", { class: "confirm lock" }, h("strong", {}, "OK to make this change?"), h("pre", {}, ev.preview || ev.name),
      h("div", { class: "row" }, note,
        h("button", { onclick: (e) => busy(e.target, () => resolve(card, ev.message_id, false, note.value)) }, "No"),
        h("button", { class: "primary", onclick: (e) => busy(e.target, () => resolve(card, ev.message_id, true, null)) }, "Yes, do it")));
    return card;
  }

  async function resolve(card, messageId, approve, note) {
    card.replaceChildren(h("div", { class: "small muted" }, approve ? "Approved." : "Declined."));
    await follow({ path: `/api/chat/threads/${thread}/confirm/${messageId}`, body: { approve, note: note || null } });
  }

  // Show a reply as it happens. With `start`, this begins the reply; without, it follows one in progress.
  async function follow(start = null) {
    controller?.abort();
    const mine = controller = new AbortController();
    sendBtn.disabled = true; stopBtn.hidden = false;
    let bubble = null, text = "";
    const status = h("div", { class: "toolline working-line" }, "Thinking…");
    log.append(status);
    const scroll = () => { log.scrollTop = log.scrollHeight; };
    const finish = () => { if (bubble) bubble.replaceWith(richText(text)); bubble = null; text = ""; };
    try {
      for await (const ev of reply(thread, { start, signal: mine.signal })) {
        if (ev.type === "mode") status.textContent = `${ev.title}…`;
        else if (ev.type === "reconnecting") status.textContent = "Reconnecting…";
        else if (ev.type === "text") {
          if (!bubble) { bubble = h("div", { class: "bubble" }); log.insertBefore(h("div", { class: "msg assistant" }, bubble), status); }
          text += ev.text;
          bubble.textContent = text;
        } else if (ev.type === "tool") {
          finish();
          if (ev.status === "running") status.textContent = `${toolLabel(ev.name)}…`;
          else log.insertBefore(h("div", { class: `toolline ${ev.status === "error" ? "error" : ""}` }, ev.status === "error" ? "✕ " : "✓ ",
            toolLabel(ev.name), ev.status === "error" && ev.summary ? `: ${ev.summary.slice(0, 160)}` : ""), status);
        } else if (ev.type === "confirm") { finish(); log.insertBefore(confirmCard(ev), status); }
        else if (ev.type === "notice" && ev.transient) status.textContent = ev.text; // retries: show, don't keep
        else if (ev.type === "notice") log.insertBefore(h("div", { class: "toolline error" }, ev.text), status);
        else if (ev.type === "error") log.insertBefore(h("div", { class: "flash err" }, ev.message), status);
        scroll();
      }
    } catch (e) {
      if (e.name !== "AbortError") log.insertBefore(h("div", { class: "flash err" }, e.message), status);
    } finally {
      finish();
      status.remove();
      if (controller === mine) { sendBtn.disabled = false; stopBtn.hidden = true; controller = null; }
      changed();
      scroll();
    }
  }

  async function send() {
    const textValue = input.value.trim() || (mode === "week" ? WEEK_REQUEST : "");
    if (!textValue || controller) return;
    sendBtn.disabled = true;
    try {
      if (!thread) {
        const t = await post("/api/chat/threads", { mode });
        thread = t.thread_id;
        onThread(thread);
        clear(log);
      }
    } catch (e) { toast(e.message, "err"); sendBtn.disabled = false; return; }
    input.value = "";
    log.querySelector(".task-picker")?.remove();
    log.append(h("div", { class: "msg user" }, h("div", { class: "bubble" }, textValue)));
    await follow({ path: `/api/chat/threads/${thread}/messages`, body: { text: textValue, mode } });
    loadThreads().catch(() => {});
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && matchMedia("(pointer: fine)").matches) { e.preventDefault(); send(); } });
  threadSelect.addEventListener("change", () => open(threadSelect.value).catch((e) => toast(e.message, "err")));

  root.append(h("div", { class: "chat" },
    h("div", { class: "chat-head row" }, threadSelect,
      h("button", { class: "link", onclick: () => { threadSelect.value = ""; open(null); } }, "New"),
      h("button", { class: "link", title: "Delete this conversation", "aria-label": "Delete this conversation", onclick: async () => {
        if (!thread || !confirm("Delete this conversation?")) return;
        await del(`/api/chat/threads/${thread}`); toast("Deleted."); await loadThreads(); open(null);
      } }, "🗑")),
    log,
    h("div", { class: "composer" }, input,
      h("div", { class: "row", style: { marginTop: "6px" } }, taskSelect, h("span", { class: "spacer" }), stopBtn, sendBtn),
      h("p", { class: "small muted", style: { margin: "6px 0 0" } }, "Notion changes ask first. Only Quick Shop adds to the cart directly."))));
  setMode(mode);
  loadThreads().then(() => open(thread)).catch((e) => log.append(h("div", { class: "flash err" }, e.message)));
  return { cleanup: () => controller?.abort() };
}
