import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { cn } from '@/lib/utils'

import {
  fetchAssistantStatus,
  sendAssistantMessages,
  type AssistantMessage,
} from './api'
import { ChatInput } from './ChatInput'

/** The initial question the homepage launcher forwards via router state. */
function readInitial(state: unknown): string | null {
  if (typeof state === 'object' && state !== null && 'initial' in state) {
    const value = (state as { initial: unknown }).initial
    return typeof value === 'string' ? value : null
  }
  return null
}

/** The assistant chat: an ephemeral thread over one browser session. Every turn
 * posts the whole conversation and nothing is stored (ADR 0016). */
export function AssistantPage() {
  const location = useLocation()
  const initial = readInitial(location.state)
  const [messages, setMessages] = useState<AssistantMessage[]>([])
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [configured, setConfigured] = useState<boolean | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchAssistantStatus()
      .then((status) => {
        if (!cancelled) setConfigured(status.configured)
      })
      .catch(() => {
        if (!cancelled) setConfigured(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const runTurn = useCallback(
    async (text: string, history: AssistantMessage[]) => {
      setError(null)
      const withUser: AssistantMessage[] = [
        ...history,
        { role: 'user', content: text },
      ]
      setMessages(withUser)
      setPending(true)
      try {
        const { reply } = await sendAssistantMessages(withUser)
        setMessages([...withUser, { role: 'assistant', content: reply }])
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setPending(false)
      }
    },
    [],
  )

  const started = useRef(false)
  useEffect(() => {
    if (configured !== true || started.current || initial === null) return
    started.current = true
    void runTurn(initial, [])
  }, [configured, initial, runTurn])

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Assistant</h1>
        <p className="text-sm text-muted-foreground">
          Ask why an order shipped the way it did. Grounded in this order&apos;s
          own timeline and allocation trace - never a guess.
        </p>
      </div>

      {messages.length > 0 && (
        <div className="flex flex-col gap-3">
          {messages.map((message, index) => (
            <div
              key={index}
              className={cn(
                'max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap',
                message.role === 'user'
                  ? 'self-end bg-primary text-primary-foreground'
                  : 'self-start bg-muted',
              )}
            >
              {message.content}
            </div>
          ))}
          {pending && (
            <div className="self-start text-sm text-muted-foreground">
              Thinking…
            </div>
          )}
        </div>
      )}

      {error !== null && (
        <p className="text-sm text-destructive" role="alert">
          {error}
        </p>
      )}

      {configured === false ? (
        <p className="text-sm text-muted-foreground">
          The assistant isn&apos;t configured on this install.
        </p>
      ) : (
        <ChatInput
          onSubmit={(text) => void runTurn(text, messages)}
          disabled={configured !== true}
          pending={pending}
          autoFocus
        />
      )}
    </div>
  )
}
