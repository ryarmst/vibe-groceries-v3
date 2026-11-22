import { mountChat } from "../lib/chat.js";
import { h } from "../lib/ui.js";

export async function render(el, [threadId], query) {
  const box = h("div", { class: "card", style: { padding: 0, overflow: "hidden" } });
  el.append(box);
  mountChat(box, { threadId: threadId || null, mode: query.mode || "auto", onThread: (id) => history.replaceState(null, "", id ? `#/chat/${id}` : "#/chat") });
  box.querySelector(".chat").style.height = "calc(100vh - 110px)";
}
