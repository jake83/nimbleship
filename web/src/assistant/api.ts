/** The AI assistant edge (ADR 0016). The conversation is ephemeral: every turn
 * posts the full thread and nothing is stored, so each answer is against live data. */

export interface AssistantMessage {
  role: 'user' | 'assistant'
  content: string
}

/** An API refusal with the server's explanation (FastAPI `detail`). */
export class AssistantError extends Error {
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
    throw new AssistantError(response.status, message)
  }
  return (await response.json()) as T
}

/** Whether the assistant is configured, so a surface can disable its input. */
export function fetchAssistantStatus(): Promise<{ configured: boolean }> {
  return request('/api/assistant/status')
}

/** Run the tool-use loop over the whole conversation and return the answer. */
export function sendAssistantMessages(
  messages: AssistantMessage[],
): Promise<{ reply: string }> {
  return request('/api/assistant/messages', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  })
}
