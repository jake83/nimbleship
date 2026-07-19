import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { ChatInput } from '@/assistant/ChatInput'
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { createDraft, fetchActiveRulebook } from '@/rulebook/api'
import {
  dryRunWorkingCopy,
  fetchBuilderStatus,
  sendBuilderMessages,
  type BuilderDryRunOutcome,
  type BuilderMessage,
} from '@/rulebook/builder-api'
import type { ServiceDeclaration } from '@/rulebook/types'

/** The optional constraint-ish fields the AI can set, summarised so an operator sees
 * a restriction (a proposition, a group, an area, a size limit) it added before
 * saving - the flat columns alone would hide it. */
function restrictions(service: ServiceDeclaration): string {
  const parts: string[] = []
  if (service.propositions.length > 0)
    parts.push(`propositions: ${service.propositions.join(', ')}`)
  if (service.service_groups.length > 0)
    parts.push(`groups: ${service.service_groups.join(', ')}`)
  if (service.areas_served !== null)
    parts.push(`areas served: ${service.areas_served.join(', ')}`)
  if (service.areas_blocked.length > 0)
    parts.push(`areas blocked: ${service.areas_blocked.join(', ')}`)
  if (service.max_dimension_cm !== null)
    parts.push(`max dim ${service.max_dimension_cm}cm`)
  if (service.max_girth_cm !== null)
    parts.push(`max girth ${service.max_girth_cm}cm`)
  return parts.join('; ')
}

function WorkingCopyTable({ services }: { services: ServiceDeclaration[] }) {
  if (services.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No services yet. Ask the builder to add one.
      </p>
    )
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Code</TableHead>
          <TableHead>Carrier</TableHead>
          <TableHead>Weight (kg)</TableHead>
          <TableHead>Countries</TableHead>
          <TableHead>Cost</TableHead>
          <TableHead>Tie-break</TableHead>
          <TableHead>Restrictions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {services.map((service) => {
          const summary = restrictions(service)
          return (
            <TableRow key={service.code}>
              <TableCell className="font-mono">{service.code}</TableCell>
              <TableCell>{service.carrier}</TableCell>
              <TableCell>
                {service.weight_min_kg} to {service.weight_max_kg}
              </TableCell>
              <TableCell>{service.countries.join(', ')}</TableCell>
              <TableCell>{service.cost}</TableCell>
              <TableCell>{service.tie_break_order}</TableCell>
              <TableCell className="text-xs text-muted-foreground">
                {summary === '' ? '-' : summary}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/** The AI rules builder (ADR 0017): a split view - a conversation on the left edits
 * an in-memory working copy of the rulebook shown on the right. The builder never
 * publishes; saving hands the working copy to the existing draft rails, where the
 * operator dry-runs and publishes it. */
export function BuilderPage() {
  const navigate = useNavigate()
  const [messages, setMessages] = useState<BuilderMessage[]>([])
  // null until the live rulebook seed loads. The client sends whatever it holds each
  // turn, and an empty [] is a legal working copy server-side, so sending before the
  // seed lands would silently start the builder from scratch instead of the live
  // rulebook - the input stays disabled until this is non-null.
  const [services, setServices] = useState<ServiceDeclaration[] | null>(null)
  const [seedError, setSeedError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [author, setAuthor] = useState('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [dryRun, setDryRun] = useState<BuilderDryRunOutcome | null>(null)
  const [dryRunning, setDryRunning] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchBuilderStatus()
      .then((status) => {
        if (!cancelled) setConfigured(status.configured)
      })
      .catch(() => {
        if (!cancelled) setConfigured(false)
      })
    // Seed the working copy from what is shipping today, so edits start from the live
    // rulebook. A load failure is surfaced rather than silently leaving an empty copy.
    fetchActiveRulebook()
      .then((rulebook) => {
        if (!cancelled) setServices(rulebook.services)
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setSeedError(
            caught instanceof Error ? caught.message : String(caught),
          )
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const runTurn = useCallback(
    async (text: string) => {
      if (services === null) return // seed not loaded; the input is disabled anyway
      setError(null)
      const withUser: BuilderMessage[] = [
        ...messages,
        { role: 'user', content: text },
      ]
      setMessages(withUser)
      setPending(true)
      try {
        const turn = await sendBuilderMessages(withUser, services)
        setMessages([...withUser, { role: 'assistant', content: turn.reply }])
        setServices(turn.services)
        // The prior preview is stale once the copy changes.
        setDryRun(null)
        setDryRunError(null)
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setPending(false)
      }
    },
    [messages, services],
  )

  async function preview() {
    if (services === null) return
    setDryRunning(true)
    setDryRunError(null)
    try {
      setDryRun(await dryRunWorkingCopy(services))
    } catch (caught) {
      setDryRun(null)
      setDryRunError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setDryRunning(false)
    }
  }

  const authorError =
    author.trim() === ''
      ? 'An author is required.'
      : author.trim().length > 64
        ? 'Author must be 64 characters or fewer.'
        : null
  const canSave =
    services !== null && services.length > 0 && authorError === null && !saving

  async function save() {
    if (services === null) return
    setSaving(true)
    setSaveError(null)
    try {
      const created = await createDraft(
        services,
        author.trim(),
        description.trim() === '' ? null : description.trim(),
      )
      navigate(`/rulebook/versions/${created.version}`)
    } catch (caught) {
      setSaveError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Rules builder</h1>
        <p className="text-sm text-muted-foreground">
          Describe the change you want. The builder edits a working copy of the
          rulebook - it never publishes. Save to create a draft you can dry-run and
          publish.
        </p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle>Conversation</CardTitle>
            <CardDescription>
              Ask for a change; the builder makes one granular edit at a time.
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
            {seedError !== null && (
              <p className="text-sm text-destructive" role="alert">
                Couldn&apos;t load the current rulebook: {seedError}
              </p>
            )}
            {configured === false ? (
              <p className="text-sm text-muted-foreground">
                The rules builder isn&apos;t configured on this install.
              </p>
            ) : (
              <ChatInput
                onSubmit={(text) => void runTurn(text)}
                disabled={configured !== true || services === null}
                pending={pending}
                placeholder="e.g. add a next-day service for France up to 10kg…"
                ariaLabel="Message the rules builder"
              />
            )}
          </CardContent>
        </Card>

        <Card className="flex flex-col">
          <CardHeader>
            <CardTitle>Working copy</CardTitle>
            <CardDescription>
              {services === null
                ? 'Loading…'
                : `${services.length} service${services.length === 1 ? '' : 's'}. Not saved until you create a draft.`}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-4">
            {services === null ? (
              <p className="text-sm text-muted-foreground">
                Loading the current rulebook…
              </p>
            ) : (
              <WorkingCopyTable services={services} />
            )}

            {services !== null && (
              <div className="flex flex-col gap-2 border-t pt-4">
                <div className="flex items-center gap-3">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => void preview()}
                    disabled={services.length === 0 || dryRunning}
                  >
                    {dryRunning ? 'Previewing…' : 'Preview impact'}
                  </Button>
                  <span className="text-xs text-muted-foreground">
                    Replay recent orders through this working copy.
                  </span>
                </div>
                {dryRunError !== null && (
                  <p className="text-sm text-destructive" role="alert">
                    {dryRunError}
                  </p>
                )}
                {dryRun !== null && (
                  <div className="flex flex-col gap-1">
                    <p className="text-sm font-medium">
                      {dryRun.changed} of {dryRun.total} recent order
                      {dryRun.total === 1 ? '' : 's'} would change service.
                    </p>
                    {dryRun.results
                      .filter((result) => result.changed)
                      .slice(0, 10)
                      .map((result) => (
                        <p
                          key={result.order_number}
                          className="text-xs text-muted-foreground"
                        >
                          <span className="font-mono">{result.order_number}</span>
                          : {result.current_service ?? 'no allocation'} →{' '}
                          {result.draft_service ?? 'no allocation'}
                        </p>
                      ))}
                  </div>
                )}
              </div>
            )}

            <div className="mt-auto flex flex-col gap-3 border-t pt-4">
              <div className="grid gap-1.5">
                <Label htmlFor="builder-author">Author</Label>
                <Input
                  id="builder-author"
                  value={author}
                  aria-invalid={author !== '' && authorError ? true : undefined}
                  onChange={(event) => setAuthor(event.target.value)}
                />
                {author !== '' && authorError && (
                  <p className="text-xs text-destructive">{authorError}</p>
                )}
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="builder-description">Description (optional)</Label>
                <Input
                  id="builder-description"
                  value={description}
                  maxLength={280}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </div>
              {saveError !== null && (
                <p className="text-sm text-destructive" role="alert">
                  {saveError}
                </p>
              )}
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
