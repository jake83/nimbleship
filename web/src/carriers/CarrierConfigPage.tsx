import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

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
import { fetchCarrierConfig, replaceCarrierConfig } from '@/carriers/api'

/** One editable row. Structured values edit as JSON text so a nested config
 * survives the round-trip instead of being flattened to a string. `structured`
 * is operator-visible and toggleable: no heuristic can tell a nested config from
 * a literal value that merely looks like one (a brace-wrapped GUID). */
interface Entry {
  key: string
  text: string
  structured: boolean
  /** Referenced by the active definition but not stored yet. */
  needed: boolean
}

function toEntries(
  config: Record<string, unknown>,
  missing: string[],
): Entry[] {
  // A stored key can still be missing (e.g. a null value renders as nothing at
  // booking), so needed is judged from the server's list, never from presence.
  const stored = Object.entries(config).map(([key, value]) => ({
    key,
    // A stored null is "not provided" (the domain's own reading): render it
    // blank rather than as the text "null" pretending a value exists.
    text:
      value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value),
    structured: typeof value !== 'string' && value !== null,
    needed: missing.includes(key),
  }))
  const placeholders = missing
    .filter((key) => !(key in config) && !key.includes('.'))
    .map((key) => ({ key, text: '', structured: false, needed: true }))
  return [...stored, ...placeholders]
}

/** Dotted missing paths (config.depot.code, config.hosts.0) live inside a
 * containing key's JSON - a flat input would save a junk top-level key with a
 * literal dot that the renderer never reads. */
function nestedMissing(missing: string[]): string[] {
  return missing.filter((key) => key.includes('.'))
}

/** The carrier config surface: credentials and per-install settings, straight to
 * Carrier Config - never through a model (ADR 0018). Saving replaces the whole
 * row, so a removed key really goes away. */
export function CarrierConfigPage() {
  const { carrier } = useParams()
  const [entries, setEntries] = useState<Entry[] | null>(null)
  const [missing, setMissing] = useState<string[]>([])
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Values are credentials: masked by default, revealed per row on demand.
  const [revealed, setRevealed] = useState<ReadonlySet<string>>(new Set())

  useEffect(() => {
    if (carrier === undefined) return
    let cancelled = false
    fetchCarrierConfig(carrier)
      .then((read) => {
        if (cancelled) return
        setEntries(toEntries(read.config, read.missing))
        setMissing(read.missing)
      })
      .catch((caught: unknown) => {
        if (!cancelled)
          setError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [carrier])

  if (carrier === undefined) return null
  if (error !== null && entries === null) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    )
  }
  if (entries === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  function setText(key: string, text: string) {
    setSaved(false)
    setEntries(
      (current) =>
        current?.map((entry) =>
          entry.key === key ? { ...entry, text } : entry,
        ) ?? null,
    )
  }

  function toggleStructured(key: string) {
    setSaved(false)
    setEntries(
      (current) =>
        current?.map((entry) =>
          entry.key === key ? { ...entry, structured: !entry.structured } : entry,
        ) ?? null,
    )
  }

  function removeEntry(key: string) {
    setSaved(false)
    setEntries((current) => current?.filter((entry) => entry.key !== key) ?? null)
  }

  function addEntry() {
    const key = newKey.trim()
    if (key === '' || entries === null) return
    if (entries.some((entry) => entry.key === key)) {
      setError(`'${key}' is already listed.`)
      return
    }
    setError(null)
    setSaved(false)
    // Default from the shape, but only as a default - the toggle on the row
    // corrects a literal value that merely looks like JSON (a braced GUID).
    const structured = /^[[{]/.test(newValue.trim())
    setEntries([...entries, { key, text: newValue, structured, needed: false }])
    setNewKey('')
    setNewValue('')
  }

  async function save() {
    if (entries === null) return
    const payload: Record<string, unknown> = {}
    for (const entry of entries) {
      // A needed key left blank stays unstored rather than saving an empty string.
      if (entry.needed && entry.text === '') continue

      if (entry.structured) {
        try {
          payload[entry.key] = JSON.parse(entry.text)
        } catch {
          setError(
            `'${entry.key}' is not valid JSON - fix it, or switch the row to text.`,
          )
          return
        }
      } else {
        payload[entry.key] = entry.text
      }
    }
    setSaving(true)
    setError(null)
    try {
      const result = await replaceCarrierConfig(carrier ?? '', payload)
      setMissing(result.missing)
      setEntries(toEntries(payload, result.missing))
      setSaved(true)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            {carrier} config
          </h1>
          <p className="text-sm text-muted-foreground">
            Credentials and per-install settings the definition reads as config.*
          </p>
        </div>
        <Button
          nativeButton={false}
          variant="outline"
          render={<Link to="/carriers" />}
        >
          All carriers
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Stored values</CardTitle>
          <CardDescription>
            Values live only in Carrier Config - the AI builder never sees them
            (documentation shows a config.* reference instead).
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {entries.length === 0 && (
            <p className="text-sm text-muted-foreground">Nothing stored yet.</p>
          )}
          {entries.map((entry) => (
            <div key={entry.key} className="grid gap-1.5">
              <div className="flex items-center gap-2">
                <Label htmlFor={`config-${entry.key}`}>{entry.key}</Label>
                {entry.needed && (
                  <Badge variant="secondary">
                    required by the active definition
                  </Badge>
                )}
                <Button
                  variant="ghost"
                  size="xs"
                  aria-label={`Save ${entry.key} as ${entry.structured ? 'text' : 'JSON'}`}
                  onClick={() => toggleStructured(entry.key)}
                >
                  <Badge variant={entry.structured ? 'default' : 'outline'}>
                    {entry.structured ? 'JSON' : 'text'}
                  </Badge>
                </Button>
              </div>
              <div className="flex items-center gap-2">
                <Input
                  id={`config-${entry.key}`}
                  type={revealed.has(entry.key) ? 'text' : 'password'}
                  value={entry.text}
                  onChange={(event) => setText(entry.key, event.target.value)}
                />
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`${revealed.has(entry.key) ? 'Hide' : 'Show'} ${entry.key}`}
                  onClick={() =>
                    setRevealed((current) => {
                      const next = new Set(current)
                      if (next.has(entry.key)) next.delete(entry.key)
                      else next.add(entry.key)
                      return next
                    })
                  }
                >
                  {revealed.has(entry.key) ? 'Hide' : 'Show'}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Remove ${entry.key}`}
                  onClick={() => removeEntry(entry.key)}
                >
                  Remove
                </Button>
              </div>
            </div>
          ))}

          {nestedMissing(missing).length > 0 && (
            <p className="text-sm text-muted-foreground">
              The active definition also needs nested values:{' '}
              <span className="font-mono text-xs">
                {nestedMissing(missing).join(', ')}
              </span>{' '}
              - edit the containing key&apos;s JSON (add it below as{' '}
              <span className="font-mono text-xs">{'{...}'}</span> if absent).
            </p>
          )}

          <div className="flex items-end gap-2 border-t pt-4">
            <div className="grid flex-1 gap-1.5">
              <Label htmlFor="config-new-key">New key</Label>
              <Input
                id="config-new-key"
                value={newKey}
                onChange={(event) => setNewKey(event.target.value)}
                placeholder="e.g. account_number"
              />
            </div>
            <div className="grid flex-1 gap-1.5">
              <Label htmlFor="config-new-value">New value</Label>
              <Input
                id="config-new-value"
                value={newValue}
                onChange={(event) => setNewValue(event.target.value)}
              />
            </div>
            <Button variant="outline" onClick={addEntry}>
              Add
            </Button>
          </div>

          {error !== null && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          {saved && (
            <p className="text-sm font-medium">
              Saved.
              {missing.length > 0 &&
                ` The active definition still needs: ${missing.join(', ')}.`}
            </p>
          )}
          <div>
            <Button disabled={saving} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save configuration'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
