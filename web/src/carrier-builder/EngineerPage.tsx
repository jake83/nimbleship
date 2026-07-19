import { useRef, useState } from 'react'

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
import { Textarea } from '@/components/ui/textarea'
import {
  fetchBlockers,
  resolveBlocker,
  type Blocker,
} from '@/carrier-builder/api'

/** The engineer's technical surface for Handoff blockers (ADR 0018): work the
 * queue here - a plugin to build, a decision to record - not the operator's chat. */
export function EngineerPage() {
  const [carrier, setCarrier] = useState('')
  const [blockers, setBlockers] = useState<Blocker[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [resolutions, setResolutions] = useState<Record<number, string>>({})
  const [resolveError, setResolveError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Two loads can race (retype the carrier, click again before the first lands);
  // only the latest may apply, or a slow response shows the wrong carrier's queue.
  const loadSeq = useRef(0)

  async function load() {
    const seq = ++loadSeq.current
    setLoadError(null)
    setResolveError(null)
    try {
      const queue = await fetchBlockers(carrier.trim())
      if (seq === loadSeq.current) setBlockers(queue)
    } catch (caught) {
      if (seq === loadSeq.current) {
        setBlockers(null)
        setLoadError(caught instanceof Error ? caught.message : String(caught))
      }
    }
  }

  async function resolve(blocker: Blocker) {
    const resolution = (resolutions[blocker.id] ?? '').trim()
    if (resolution === '') return
    setBusy(true)
    setResolveError(null)
    try {
      const updated = await resolveBlocker(blocker.id, resolution)
      setBlockers(
        (current) =>
          current?.map((b) => (b.id === updated.id ? updated : b)) ?? null,
      )
    } catch (caught) {
      setResolveError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          Engineer handoffs
        </h1>
        <p className="text-sm text-muted-foreground">
          Gaps the carrier builder parked for engineering: a plugin to build, or
          a decision the docs don&apos;t answer. Record the answer here; the next
          builder turn applies it. A carrier can&apos;t publish while a handoff
          is open.
        </p>
      </div>

      <div className="flex max-w-md items-end gap-2">
        <div className="grid flex-1 gap-1.5">
          <Label htmlFor="engineer-carrier">Carrier</Label>
          <Input
            id="engineer-carrier"
            value={carrier}
            placeholder="e.g. acme"
            onChange={(event) => setCarrier(event.target.value)}
          />
        </div>
        <Button
          type="button"
          onClick={() => void load()}
          disabled={carrier.trim() === ''}
        >
          Load handoffs
        </Button>
      </div>

      {loadError !== null && (
        <p className="text-sm text-destructive" role="alert">
          {loadError}
        </p>
      )}
      {resolveError !== null && (
        <p className="text-sm text-destructive" role="alert">
          {resolveError}
        </p>
      )}
      {blockers !== null && blockers.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No handoffs for this carrier.
        </p>
      )}

      {blockers !== null &&
        blockers.map((blocker) => (
          <Card key={blocker.id}>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <Badge
                  variant={blocker.status === 'open' ? 'destructive' : 'default'}
                >
                  {blocker.status}
                </Badge>
                {blocker.title}
              </CardTitle>
              <CardDescription>
                {blocker.kind === 'needs_plugin'
                  ? `Needs a plugin${blocker.plugin_name !== null ? `: ${blocker.plugin_name}` : ''}`
                  : 'Needs a decision'}
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3 text-sm">
              <p className="whitespace-pre-wrap">{blocker.detail}</p>
              {blocker.status === 'resolved' ? (
                <p className="font-medium">Resolved: {blocker.resolution}</p>
              ) : (
                <div className="flex flex-col gap-2">
                  <Label htmlFor={`resolution-${blocker.id}`}>
                    Resolution (a decision, or &quot;shipped in vX&quot;)
                  </Label>
                  <Textarea
                    id={`resolution-${blocker.id}`}
                    value={resolutions[blocker.id] ?? ''}
                    onChange={(event) =>
                      setResolutions((current) => ({
                        ...current,
                        [blocker.id]: event.target.value,
                      }))
                    }
                  />
                  <div>
                    <Button
                      type="button"
                      onClick={() => void resolve(blocker)}
                      disabled={
                        busy || (resolutions[blocker.id] ?? '').trim() === ''
                      }
                    >
                      Resolve
                    </Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
    </div>
  )
}
