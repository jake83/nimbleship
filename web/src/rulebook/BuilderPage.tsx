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
  fetchBuilderStatus,
  sendBuilderMessages,
  type BuilderMessage,
} from '@/rulebook/builder-api'
import type { ServiceDeclaration } from '@/rulebook/types'

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
        </TableRow>
      </TableHeader>
      <TableBody>
        {services.map((service) => (
          <TableRow key={service.code}>
            <TableCell className="font-mono">{service.code}</TableCell>
            <TableCell>{service.carrier}</TableCell>
            <TableCell>
              {service.weight_min_kg} to {service.weight_max_kg}
            </TableCell>
            <TableCell>{service.countries.join(', ')}</TableCell>
            <TableCell>{service.cost}</TableCell>
            <TableCell>{service.tie_break_order}</TableCell>
          </TableRow>
        ))}
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
  const [services, setServices] = useState<ServiceDeclaration[]>([])
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [configured, setConfigured] = useState<boolean | null>(null)
  const [author, setAuthor] = useState('')
  const [description, setDescription] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchBuilderStatus()
      .then((status) => {
        if (!cancelled) setConfigured(status.configured)
      })
      .catch(() => {
        if (!cancelled) setConfigured(false)
      })
    // Seed the shown working copy from what is shipping today, so the panel is
    // populated before the first turn (the same starting point the API seeds from).
    fetchActiveRulebook()
      .then((rulebook) => {
        if (!cancelled) setServices(rulebook.services)
      })
      .catch(() => {
        // A load failure leaves an empty copy; the builder can still add services.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const runTurn = useCallback(
    async (text: string) => {
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
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught))
      } finally {
        setPending(false)
      }
    },
    [messages, services],
  )

  const authorError =
    author.trim() === ''
      ? 'An author is required.'
      : author.trim().length > 64
        ? 'Author must be 64 characters or fewer.'
        : null
  const canSave = services.length > 0 && authorError === null && !saving

  async function save() {
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
            {configured === false ? (
              <p className="text-sm text-muted-foreground">
                The rules builder isn&apos;t configured on this install.
              </p>
            ) : (
              <ChatInput
                onSubmit={(text) => void runTurn(text)}
                disabled={configured !== true}
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
              {services.length} service{services.length === 1 ? '' : 's'}. Not saved
              until you create a draft.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-4">
            <WorkingCopyTable services={services} />

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
