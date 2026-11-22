import { mountChat } from "../lib/chat.js";
import { h } from "../lib/ui.js";

export async function render(el, [threadId], query) {
  const box = h("div", { class: "card chat-card" });
  el.append(box);
  return mountChat(box, { threadId: threadId || null, mode: query.mode || "auto",
    onThread: (id) => history.replaceState(null, "", id ? `#/chat/${id}` : "#/chat") });
}
