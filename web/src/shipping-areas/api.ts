/** Shipping Areas admin (CONTEXT.md): the named geography is the thing, its
 * postcode prefixes are the definition. The server normalises prefixes (uppercase,
 * trimmed, deduplicated) at the write edge - the surface sends what the operator
 * typed and displays what came back. */

export interface ShippingArea {
  code: string
  name: string
  country: string
  prefixes: string[]
}

/** An API refusal with the server's explanation (FastAPI `detail`). */
export class ShippingAreaError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/** FastAPI's `detail` is a plain string for our HTTPExceptions, but an array of
 * {loc, msg, ...} for a 422 validation error - surface the first message either way. */
function detailMessage(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first: unknown = detail[0]
    if (
      typeof first === 'object' &&
      first !== null &&
      'msg' in first &&
      typeof first.msg === 'string'
    ) {
      return first.msg
    }
  }
  return null
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let message = `request failed (${response.status})`
    try {
      const body: unknown = await response.json()
      if (typeof body === 'object' && body !== null && 'detail' in body) {
        message = detailMessage(body.detail) ?? message
      }
    } catch {
      // Non-JSON error body; keep the generic message.
    }
    throw new ShippingAreaError(response.status, message)
  }
  return (await response.json()) as T
}

export function fetchShippingAreas(): Promise<ShippingArea[]> {
  return request('/api/shipping-areas')
}

export function fetchShippingArea(code: string): Promise<ShippingArea> {
  return request(`/api/shipping-areas/${encodeURIComponent(code)}`)
}

export function createShippingArea(area: ShippingArea): Promise<ShippingArea> {
  return request('/api/shipping-areas', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(area),
  })
}

/** The code is the area's identity (rulebook declarations reference it) and never
 * changes; name, country, and the full prefix list are replaced. */
export function updateShippingArea(
  code: string,
  fields: { name: string; country: string; prefixes: string[] },
): Promise<ShippingArea> {
  return request(`/api/shipping-areas/${encodeURIComponent(code)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  })
}
