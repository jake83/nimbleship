import type { DryRunResult, ServiceDeclaration } from '@/rulebook/types'

/** The AI rules builder edge (ADR 0017). Each turn posts the whole conversation and
 * the working copy so far; the reply carries the working copy after the model's
 * edits. Nothing is saved here - the operator commits the copy as a draft through
 * the existing rulebook rails. */

export interface BuilderMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface BuilderTurn {
  reply: string
  services: ServiceDeclaration[]
}

/** Impact of replaying the working copy over recent orders. No rulebook_version -
 * the copy is unsaved. */
export interface BuilderDryRunOutcome {
  total: number
  changed: number
  results: DryRunResult[]
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

/** Whether the builder is configured, so a surface can disable its input. */
export function fetchBuilderStatus(): Promise<{ configured: boolean }> {
  return request('/api/rulebook/builder/status')
}

/** Run one builder turn against the working copy and return the reply plus the
 * edited copy. */
export function sendBuilderMessages(
  messages: BuilderMessage[],
  services: ServiceDeclaration[],
): Promise<BuilderTurn> {
  return request('/api/rulebook/builder/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, services }),
  })
}

/** Preview the working copy's impact over recent orders before saving a draft. Pure
 * allocation server-side - no model, so it works whether or not a key is configured. */
export function dryRunWorkingCopy(
  services: ServiceDeclaration[],
): Promise<BuilderDryRunOutcome> {
  return request('/api/rulebook/builder/dry-run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ services }),
  })
}
