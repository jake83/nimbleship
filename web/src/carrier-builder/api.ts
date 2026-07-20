/** The AI carrier builder edge (ADR 0018). Each turn posts the whole conversation and
 * the working definition so far; the reply carries the definition after the model's
 * edits. Nothing is saved here - the operator commits it as a draft through the
 * definition rails. */

// The working definition is a partial CarrierDefinition assembled key by key; the
// backend owns its schema, the surface only displays and round-trips it.
export type WorkingDefinition = Record<string, unknown>

export interface BuilderMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface BuilderTurn {
  reply: string
  definition: WorkingDefinition
  /** Board capability -> reason the builder pruned it as not offered (ADR 0018).
   * Board state, never definition state - it rides the working copy each turn. */
  not_applicable?: Record<string, string>
}

export interface CheckOutcome {
  valid: boolean
  errors: string[]
}

/** A Handoff blocker (ADR 0018): a technical gap parked for the engineer. */
export interface Blocker {
  id: number
  carrier: string
  kind: 'needs_plugin' | 'needs_decision'
  title: string
  detail: string
  plugin_name: string | null
  status: 'open' | 'resolved'
  resolution: string | null
  created_at: string
  resolved_at: string | null
}

/** An API refusal with the server's explanation (FastAPI `detail`). */
export class BuilderError extends Error {
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
    throw new BuilderError(response.status, message)
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

/** Whether the builder is configured, so the surface can disable its input. */
export function fetchBuilderStatus(): Promise<{ configured: boolean }> {
  return request('/api/carrier-builder/status')
}

/** Run one builder turn against the working definition, grounded in the onboarding
 * packet (documentation text; the server redacts known stored credentials before the
 * model sees it), and return the reply plus the edited copy. */
export function sendBuilderMessages(
  messages: BuilderMessage[],
  definition: WorkingDefinition,
  packet: string,
  notApplicable: Record<string, string>,
): Promise<BuilderTurn> {
  return post('/api/carrier-builder/messages', {
    messages,
    definition,
    packet,
    not_applicable: notApplicable,
  })
}

/** Store credentials for a carrier - straight to Carrier Config, never into the
 * packet or the model (ADR 0018). Merges, so adding a key keeps the rest. `missing`
 * reports the config.* keys the carrier's active definition still needs. */
export function saveCredentials(
  carrier: string,
  entries: Record<string, string>,
): Promise<{ carrier: string; status: string; missing: string[] }> {
  return request(`/api/carriers/${encodeURIComponent(carrier)}/config`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(entries),
  })
}

/** Validate the working definition and report what remains - the capability board's
 * completeness signal. Pure validation server-side; no model, no key needed. */
export function checkDefinition(
  definition: WorkingDefinition,
): Promise<CheckOutcome> {
  return post('/api/carrier-builder/check', { definition })
}

/** Commit the working definition as a draft through the existing definition rails.
 * The definition's own carrier code names the rails' carrier. */
export function createDefinitionDraft(
  carrier: string,
  definition: WorkingDefinition,
  author: string,
): Promise<{ carrier: string; version: number; status: string }> {
  return post(`/api/carriers/${encodeURIComponent(carrier)}/definitions/drafts`, {
    definition,
    author,
  })
}

/** A carrier's Handoff blockers, open and resolved - the engineer's queue and the
 * onboarding's audit trail. No model, no key needed. */
export function fetchBlockers(carrier: string): Promise<Blocker[]> {
  return request(
    `/api/carrier-builder/blockers?carrier=${encodeURIComponent(carrier)}`,
  )
}

/** Record the engineer's answer and close a blocker. A second resolve is refused
 * (409) rather than silently overwriting the recorded answer. */
export function resolveBlocker(
  blockerId: number,
  resolution: string,
): Promise<Blocker> {
  return post(`/api/carrier-builder/blockers/${blockerId}/resolve`, { resolution })
}
