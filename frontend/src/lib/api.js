// Thin fetch layer. Every call is scoped to a run with ?run=<id> so several
// views can read different runs through one server without sharing the
// server's single "attached" pointer.

const TOKEN_KEY = "kd.token";
let authHandler = () => {};

export const onAuthRequired = (fn) => (authHandler = fn);

export function getToken() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}
export function setToken(t) {
  try {
    if (t) sessionStorage.setItem(TOKEN_KEY, t);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable */
  }
}

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export function url(path, run, params) {
  const u = new URL(path, location.origin);
  if (run) u.searchParams.set("run", run);
  if (params) for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
  return u.pathname + u.search;
}

async function request(method, path, { run, params, body, signal } = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(url(path, run, params), {
      method,
      headers,
      signal,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError("offline", 0, null);
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 200) };
    }
  }
  if (res.status === 401) {
    authHandler();
    throw new ApiError("authentication required", 401, data);
  }
  if (!res.ok) throw new ApiError((data && (data.error || data.message)) || `${res.status}`, res.status, data);
  return data;
}

export const get = (path, opts) => request("GET", path, opts);
export const post = (path, body = {}, opts = {}) => request("POST", path, { ...opts, body });
export const del = (path, opts) => request("DELETE", path, opts);
