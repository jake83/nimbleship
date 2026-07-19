import { useEffect, useState } from 'react'

import { ChatInput } from '@/assistant/ChatInput'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { cn } from '@/lib/utils'
import {
  checkDefinition,
  createDefinitionDraft,
  fetchBlockers,
  fetchBuilderStatus,
  sendBuilderMessages,
  type Blocker,
  type BuilderMessage,
  type CheckOutcome,
  type WorkingDefinition,
} from '@/carrier-builder/api'

type RowState = 'drafted' | 'not started'

interface BoardRow {
  label: string
  state: RowState
  detail: string | null
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null
}

/** The operator's view of the draft (ADR 0018): capability rows, never definition
 * guts. Rows are the pieces a working integration needs; state is derived from what
 * the AI has assembled so far, and `check` supplies the remaining-work signal. */
function boardRows(definition: WorkingDefinition): BoardRow[] {
  const carrier = asString(definition.carrier)
  const name = asString(definition.name)
  const auth = definition.auth as Record<string, unknown> | undefined
  const operations =
    typeof definition.operations === 'object' && definition.operations !== null
      ? (definition.operations as Record<string, unknown>)
      : {}
  const rows: BoardRow[] = [
    {
      label: 'Carrier identity',
      state: carrier !== null && name !== null ? 'drafted' : 'not started',
      detail: carrier !== null && name !== null ? `${name} (${carrier})` : null,
    },
    {
      label: 'Authentication',
      state: auth !== undefined ? 'drafted' : 'not started',
      detail: auth !== undefined ? String(auth.scheme ?? '') : null,
    },
  ]
  const operationNames = Object.keys(operations)
  if (operationNames.length === 0) {
    rows.push({ label: 'Operations', state: 'not started', detail: null })
  } else {
    for (const operation of operationNames.sort()) {
      rows.push({ label: `Operation: ${operation}`, state: 'drafted', detail: null })
    }
  }
  return rows
}

/** The AI carrier builder (ADR 0018): a split view - a conversation on the left
 * assembles a draft carrier definition shown as a capability board on the right. The
 * builder never publishes; saving commits a draft through the definition rails. */
export function CarrierBuilderPage() {
  const [messages, setMessages] = useState<BuilderMessage[]>([])
  const [definition, setDefinition] = useState<WorkingDefinition>({})
  const [checkOutcome, setCheckOutcome] = useState<CheckOutcome | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [author, setAuthor] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)
  const [blockers, setBlockers] = useState<Blocker[]>([])

  useEffect(() => {
    let cancelled = false
    fetchBuilderStatus()
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

  async function runTurn(text: string) {
    setError(null)
    const withUser: BuilderMessage[] = [
      ...messages,
      { role: 'user', content: text },
    ]
    setMessages(withUser)
    setPending(true)
    try {
      const turn = await sendBuilderMessages(withUser, definition)
      setMessages([...withUser, { role: 'assistant', content: turn.reply }])
      setDefinition(turn.definition)
      setSaved(null) // the copy moved on; a prior save no longer describes it
      try {
        setCheckOutcome(await checkDefinition(turn.definition))
      } catch {
        setCheckOutcome(null) // non-fatal: the board just omits the signal
      }
      // The turn may have raised or consumed a blocker; refresh what's parked.
      const turnCarrier = asString(turn.definition.carrier)
      if (turnCarrier !== null) {
        try {
          setBlockers(await fetchBlockers(turnCarrier))
        } catch {
          // Non-fatal: the panel just goes stale until the next turn.
        }
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setPending(false)
    }
  }

  const carrier = asString(definition.carrier)
  const authorError =
    author.trim() === ''
      ? 'An author is required.'
      : author.trim().length > 64
        ? 'Author must be 64 characters or fewer.'
        : null
  // !pending matters: during a turn the copy on screen is about to be superseded, so
  // saving would persist the pre-turn definition while confirming success.
  const canSave =
    carrier !== null &&
    checkOutcome?.valid === true &&
    authorError === null &&
    !saving &&
    !pending

  async function save() {
    if (carrier === null) return
    setSaving(true)
    setSaveError(null)
    try {
      const created = await createDefinitionDraft(carrier, definition, author.trim())
      setSaved(`Draft v${created.version} of ${created.carrier} saved.`)
    } catch (caught) {
      setSaveError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  const rows = boardRows(definition)

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Carrier builder</h1>
        <p className="text-sm text-muted-foreground">
          Onboard a new carrier by conversation. The builder drafts the integration -
          it never publishes. Saving creates a draft on the carrier&apos;s definition
          rails.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle>Conversation</CardTitle>
            <CardDescription>
              Describe the carrier; answer the builder&apos;s questions.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-3">
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
                    Working…
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
                The carrier builder isn&apos;t configured on this install.
              </p>
            ) : (
              <ChatInput
                onSubmit={(text) => void runTurn(text)}
                disabled={configured !== true}
                pending={pending}
                placeholder="e.g. onboard Acme - they have a REST booking API…"
                ariaLabel="Message the carrier builder"
              />
            )}
          </CardContent>
        </Card>

        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle>Integration status</CardTitle>
            <CardDescription>
              What the draft covers so far. Not saved until you create a draft.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-4">
            <ul className="flex flex-col gap-2">
              {rows.map((row) => (
                <li key={row.label} className="flex items-center gap-2 text-sm">
                  <Badge
                    variant={row.state === 'drafted' ? 'default' : 'outline'}
                  >
                    {row.state}
                  </Badge>
                  <span>{row.label}</span>
                  {row.detail !== null && (
                    <span className="text-muted-foreground">- {row.detail}</span>
                  )}
                </li>
              ))}
            </ul>

            {checkOutcome !== null && !checkOutcome.valid && (
              <div className="text-sm">
                <p className="font-medium">Still needed:</p>
                <ul className="list-inside list-disc text-muted-foreground">
                  {checkOutcome.errors.map((problem) => (
                    <li key={problem}>{problem}</li>
                  ))}
                </ul>
              </div>
            )}
            {checkOutcome?.valid === true && (
              <p className="text-sm font-medium">
                The definition is complete and ready to save.
              </p>
            )}

            {blockers.length > 0 && (
              <div className="flex flex-col gap-2 border-t pt-4 text-sm">
                <p className="font-medium">Engineering handoffs</p>
                <ul className="flex flex-col gap-2">
                  {blockers.map((blocker) => (
                    <li key={blocker.id} className="flex items-start gap-2">
                      <Badge
                        variant={
                          blocker.status === 'open' ? 'destructive' : 'default'
                        }
                      >
                        {blocker.status === 'open'
                          ? 'waiting on engineering'
                          : 'answered'}
                      </Badge>
                      <span>
                        {blocker.title}
                        {blocker.status === 'resolved' &&
                          blocker.resolution !== null && (
                            <span className="text-muted-foreground">
                              {' '}
                              - {blocker.resolution}
                            </span>
                          )}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="mt-auto flex flex-col gap-3 border-t pt-4">
              <div className="grid gap-1.5">
                <Label htmlFor="carrier-builder-author">Author</Label>
                <Input
                  id="carrier-builder-author"
                  value={author}
                  aria-invalid={author !== '' && authorError ? true : undefined}
                  onChange={(event) => setAuthor(event.target.value)}
                />
                {author !== '' && authorError && (
                  <p className="text-xs text-destructive">{authorError}</p>
                )}
              </div>
              {saveError !== null && (
                <p className="text-sm text-destructive" role="alert">
                  {saveError}
                </p>
              )}
              {saved !== null && <p className="text-sm font-medium">{saved}</p>}
              <Button type="button" onClick={() => void save()} disabled={!canSave}>
                Save as draft
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
