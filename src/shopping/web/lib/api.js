// API client: same-origin JSON and streamed (SSE) calls. Cloudflare Access signs every request through its
// cookie, so there's no token handling here. The x-vibe header marks calls as coming from this app (CSRF guard).

export class ApiError extends Error {
  constructor(message, status, code) { super(message); this.status = status; this.code = code; }
}

async function request(path, { method = "GET", body, signal } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, signal, credentials: "same-origin", redirect: "manual",
      headers: { "x-vibe": "1", ...(body !== undefined ? { "content-type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Can't reach the server. Check your connection, or reload the page.", 0, "network");
  }
  // Access redirects to its login page once the sign-in expires; a fetch can't follow that, so reload.
  if (res.type === "opaqueredirect" || res.status === 401) {
    throw new ApiError("Your sign-in has expired. Reload the page to sign in again.", 401, "access_expired");
  }
  return res;
}

export async function api(path, opts = {}) {
  const res = await request(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Request failed (${res.status})`, res.status, data.code);
  return data;
}

export const get = (path) => api(path);
export const post = (path, body = {}) => api(path, { method: "POST", body });
export const put = (path, body = {}) => api(path, { method: "PUT", body });
export const del = (path) => api(path, { method: "DELETE" });

// Server-Sent Events over a POST: yields each `data:` JSON event.
export async function* stream(path, body, signal) {
  const res = await request(path, { method: "POST", body, signal });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(data.error || `Request failed (${res.status})`, res.status, data.code);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
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

// Other parts of the page (the header badge) refresh when something changes.
export function changed() { window.dispatchEvent(new Event("vibe:changed")); }
