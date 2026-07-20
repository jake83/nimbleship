/** The carriers admin surface: the catalog, and per-carrier config read/replace.
 * Values are carrier credentials and settings - they stay between the operator and
 * Carrier Config, never routed through a model (ADR 0018). */

export interface CarrierSummary {
  carrier: string
  /** Highest published definition version; null when nothing is published yet. */
  active_version: number | null
}

export interface CarrierConfigRead {
  carrier: string
  config: Record<string, unknown>
  /** config.* keys the active definition references but the stored config lacks. */
  missing: string[]
}

export class CarrierApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

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
    throw new CarrierApiError(response.status, message)
  }
  return (await response.json()) as T
}

export function fetchCarriers(): Promise<CarrierSummary[]> {
  return request('/api/carriers')
}

export function fetchCarrierConfig(carrier: string): Promise<CarrierConfigRead> {
  return request(`/api/carriers/${encodeURIComponent(carrier)}/config`)
}

/** Replace the whole config row - the page edits the full set, so removed keys
 * really go away (a PATCH would silently resurrect them). */
export function replaceCarrierConfig(
  carrier: string,
  entries: Record<string, unknown>,
): Promise<{ carrier: string; status: string; missing: string[] }> {
  return request(`/api/carriers/${encodeURIComponent(carrier)}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(entries),
  })
}
