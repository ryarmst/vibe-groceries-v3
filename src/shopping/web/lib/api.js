// API client: same-origin JSON calls and assistant replies. Cloudflare Access signs every request through its
// cookie, so there's no token handling here. The x-vibe header marks calls as coming from this app (CSRF guard).

export class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

const TIMEOUT_MS = 60_000; // No Frills can be slow, but a minute means something's wrong

async function request(path, { method = "GET", body, signal, timeout = TIMEOUT_MS } = {}) {
  const timer = timeout ? AbortSignal.timeout(timeout) : null;
  const signals = [signal, timer].filter(Boolean);
  let res;
  try {
    res = await fetch(path, {
      method, credentials: "same-origin", redirect: "manual",
      signal: signals.length > 1 ? AbortSignal.any(signals) : signals[0],
      headers: { "x-vibe": "1", ...(body !== undefined ? { "content-type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (signal?.aborted) throw e; // the page cancelled it on purpose
    if (timer?.aborted) throw new ApiError("That took too long. Try again in a moment.", 0, "timeout");
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0, "network");
  }
  // Access redirects to its sign-in page once the session expires; a fetch can't follow that, so reload.
  if (res.type === "opaqueredirect" || res.status === 401) {
    throw new ApiError("Your sign-in has expired. Reload the page to sign in again.", 401, "access_expired");
  }
  return res;
}

export async function api(path, opts = {}) {
  const res = await request(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Something went wrong (${res.status}).`, res.status, data.code);
  return data;
}

export const get = (path, opts) => api(path, opts);
export const post = (path, body = {}) => api(path, { method: "POST", body });
export const put = (path, body = {}) => api(path, { method: "PUT", body });
export const del = (path) => api(path, { method: "DELETE" });

async function* events(res) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let i;
    while ((i = buffer.indexOf("\n\n")) >= 0) {
      const chunk = buffer.slice(0, i);
      buffer = buffer.slice(i + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data:")) {
          try { yield JSON.parse(line.slice(5).trim()); } catch { /* ignore malformed */ }
        }
      }
    }
  }
}

// An assistant reply. It runs on the server, so if the connection drops (phone locked, Wi-Fi blip) this
// reconnects and carries on from the last event it saw. Pass `start` to begin a reply (a POST), or nothing
// to follow one already in progress. Yields the reply's events; ends after "done".
export async function* reply(threadId, { start = null, signal } = {}) {
  let seq = 0, attempts = 0;
  const sent = !!start;
  for (;;) {
    const path = start ? start.path : `/api/chat/threads/${threadId}/events?after=${seq}`;
    const res = await request(path, start ? { method: "POST", body: start.body, signal, timeout: 0 } : { signal, timeout: 0 })
      .catch((e) => { if (signal?.aborted || e.code === "access_expired") throw e; return null; });
    if (res && !res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new ApiError(data.error || `Something went wrong (${res.status}).`, res.status, data.code);
    }
    if (res) {
      try {
        for await (const ev of events(res)) {
          attempts = 0;
          if (ev.type === "ping") continue;
          if (ev.idle && sent && seq === 0) throw new ApiError("Your message didn't reach the server. Try again.", 0, "network");
          if (ev.seq) seq = ev.seq;
          yield ev;
          if (ev.type === "done") return;
        }
      } catch (e) {
        if (signal?.aborted || e instanceof ApiError) throw e;
      }
    }
    if (++attempts > 5) throw new ApiError("Lost the connection. The assistant keeps working: reopen the conversation in Chat to see the reply.", 0, "network");
    yield { type: "reconnecting" };
    await new Promise((r) => setTimeout(r, Math.min(1000 * attempts, 5000)));
    start = null; // never send the message twice: from now on, just follow the reply
  }
}

// Other parts of the page (the header) refresh when something changes.
export function changed() { window.dispatchEvent(new Event("vibe:changed")); }
