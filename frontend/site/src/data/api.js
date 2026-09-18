/**
 * The API client.
 *
 * Two responsibilities beyond fetching, both of which exist because of how the
 * backend behaves:
 *
 * 1. **Session handling.** The server issues an opaque token which must be
 *    echoed in a header; without it the allowance is keyed on IP, which behind
 *    carrier-grade NAT means one allowance shared across a whole mobile
 *    network. The token is kept in `localStorage` and re-minted if the server
 *    stops recognising it.
 * 2. **Structured error surfacing.** The backend returns one error shape with a
 *    code and a request id. Collapsing that into a string would discard the two
 *    things the interface needs: whether the allowance is exhausted, and what
 *    to quote when reporting a problem.
 */

const GUEST_KEY = 'solaris-guest-token';

export class ApiError extends Error {
  constructor({ code, message, status, requestId, extra }) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.requestId = requestId;
    this.extra = extra ?? {};
  }

  /** Whether the session allowance is exhausted, rather than a fault. */
  get allowanceExhausted() {
    return this.code === 'allowance_exhausted';
  }

  get isTransient() {
    return ['rate_limited', 'busy', 'budget_exhausted'].includes(this.code);
  }

  /**
   * Whether this is a server *configuration* problem rather than a fault.
   *
   * Worth distinguishing in the UI: retrying will not help, and the useful
   * response is to show the setup hint rather than a "try again" button.
   */
  get isConfiguration() {
    return this.code === 'earth_engine_unavailable';
  }

  /** Actionable setup guidance, when the server chose to include it. */
  get hint() {
    return this.extra.hint ?? '';
  }
}

let guestToken = localStorage.getItem(GUEST_KEY) ?? null;

export function currentGuestToken() {
  return guestToken;
}

async function ensureGuestToken() {
  if (guestToken) return guestToken;
  try {
    const response = await fetch('/api/auth/guest', { method: 'POST' });
    if (!response.ok) return null;
    const body = await response.json();
    guestToken = body.guest_token;
    localStorage.setItem(GUEST_KEY, guestToken);
    return guestToken;
  } catch {
    // A failure here is not fatal: without a token the server falls back to
    // keying on IP, which is worse but still works.
    return null;
  }
}

function clearGuestToken() {
  guestToken = null;
  localStorage.removeItem(GUEST_KEY);
}

async function request(path, { method = 'GET', body, signal } = {}) {
  const headers = { accept: 'application/json' };
  if (body) headers['content-type'] = 'application/json';
  const token = await ensureGuestToken();
  if (token) headers['x-solaris-guest'] = token;

  const response = await fetch(path, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });

  const cache = response.headers.get('x-cache');
  const remaining = response.headers.get('x-guest-remaining');
  const allowance = response.headers.get('x-guest-allowance');

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const error = payload?.error ?? {};
    // A 401 means the stored token is no longer known -- the server restarted,
    // most likely, since sessions are in-process. Drop it so the next call
    // mints a fresh one rather than failing indefinitely.
    if (response.status === 401) clearGuestToken();
    throw new ApiError({
      code: error.code ?? `http_${response.status}`,
      message:
        error.message ??
        payload?.detail ??
        `Request failed with status ${response.status}.`,
      status: response.status,
      requestId: payload?.request_id,
      extra: error,
    });
  }

  return {
    data: payload,
    meta: {
      cache,
      guestRemaining: remaining == null ? null : Number(remaining),
      guestAllowance: allowance == null ? null : Number(allowance),
    },
  };
}

export const api = {
  presets: () => request('/api/presets'),
  version: () => request('/api/version'),
  me: () => request('/api/auth/me'),
  yield: (payload, signal) =>
    request('/api/yield', { method: 'POST', body: payload, signal }),
  series: (payload, signal) =>
    request('/api/series', { method: 'POST', body: payload, signal }),
  tiles: (payload, signal) =>
    request('/api/tiles', { method: 'POST', body: payload, signal }),
  buildings: (payload, signal) =>
    request('/api/buildings', { method: 'POST', body: payload, signal }),
};

/**
 * Encode the current query into the URL, and read it back.
 *
 * Cheap to build and disproportionately useful: it makes every figure in a
 * write-up reproducible by link, and means a result can be sent to someone
 * rather than described to them.
 */
export function encodeQuery(params) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value != null && value !== '') search.set(key, String(value));
  });
  return search.toString();
}

export function decodeQuery(search, defaults) {
  const params = new URLSearchParams(search);
  const out = { ...defaults };
  Object.keys(defaults).forEach((key) => {
    if (!params.has(key)) return;
    const raw = params.get(key);
    const fallback = defaults[key];
    out[key] = typeof fallback === 'number' ? Number(raw) : raw;
  });
  return out;
}
