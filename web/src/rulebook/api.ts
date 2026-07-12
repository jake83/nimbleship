import type {
  ActiveRulebook,
  DryRunOutcome,
  ServiceDeclaration,
  VersionDetail,
  VersionSummary,
} from '@/rulebook/types'

/** An API refusal with the server's explanation (FastAPI `detail`). */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let message = `request failed (${response.status})`
    try {
      const body: unknown = await response.json()
      if (
        typeof body === 'object' &&
        body !== null &&
        'detail' in body &&
        typeof body.detail === 'string'
      ) {
        message = body.detail
      }
    } catch {
      // Non-JSON error body; keep the generic message.
    }
    throw new ApiError(response.status, message)
  }
  return (await response.json()) as T
}

function post<T>(url: string, payload: unknown): Promise<T> {
  return request<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
}

export function fetchActiveRulebook(): Promise<ActiveRulebook> {
  return request('/api/rulebook/active')
}

export function fetchVersions(): Promise<VersionSummary[]> {
  return request('/api/rulebook/versions')
}

export function fetchVersion(version: number): Promise<VersionDetail> {
  return request(`/api/rulebook/versions/${version}`)
}

export function createDraft(
  services: ServiceDeclaration[],
  author: string,
): Promise<VersionSummary> {
  return post('/api/rulebook/drafts', { services, author })
}

export function publishVersion(version: number): Promise<VersionSummary> {
  return post(`/api/rulebook/versions/${version}/publish`, {})
}

export function runDryRun(
  version: number,
  payload: { order_numbers?: string[]; limit?: number },
): Promise<DryRunOutcome> {
  return post(`/api/rulebook/versions/${version}/dry-run`, payload)
}
