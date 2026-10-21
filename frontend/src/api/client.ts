// Thin fetch wrapper. In dev, Vite proxies /api to the uvicorn server
// (see vite.config.ts), so requests are always same-origin.
const BASE = import.meta.env.VITE_API_BASE ?? ''

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      if (body && typeof body.detail === 'string') detail = body.detail
    } catch {
      // non-JSON error body - keep the status text
    }
    throw new Error(detail || `Request failed (${res.status})`)
  }
  return (await res.json()) as T
}

export function apiGet<T>(path: string): Promise<T> {
  return fetch(`${BASE}${path}`).then((r) => handle<T>(r))
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => handle<T>(r))
}
