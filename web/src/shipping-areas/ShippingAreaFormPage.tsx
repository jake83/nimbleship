import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

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
  createShippingArea,
  fetchShippingAreas,
  updateShippingArea,
} from '@/shipping-areas/api'

/** The operator pastes prefixes however they arrive (commas, spaces, newlines);
 * the server owns normalisation (uppercase, dedupe, sort) at the write edge. */
function splitPrefixes(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((prefix) => prefix.trim())
    .filter((prefix) => prefix !== '')
}

/** Create or edit one shipping area. The code is the area's identity - rulebook
 * rules reference it - so it is set at creation and never changes. */
export function ShippingAreaFormPage() {
  const { code } = useParams()
  const editing = code !== undefined
  const navigate = useNavigate()
  const [areaCode, setAreaCode] = useState(code ?? '')
  const [name, setName] = useState('')
  const [country, setCountry] = useState('')
  const [prefixText, setPrefixText] = useState('')
  const [loaded, setLoaded] = useState(!editing)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!editing) return
    let cancelled = false
    fetchShippingAreas()
      .then((areas) => {
        if (cancelled) return
        const area = areas.find((candidate) => candidate.code === code)
        if (area === undefined) {
          setError(`No shipping area '${code}'.`)
          return
        }
        setName(area.name)
        setCountry(area.country)
        setPrefixText(area.prefixes.join(', '))
        setLoaded(true)
      })
      .catch((caught: unknown) => {
        if (!cancelled)
          setError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [editing, code])

  const prefixes = splitPrefixes(prefixText)
  const canSave =
    !saving &&
    loaded &&
    areaCode.trim() !== '' &&
    name.trim() !== '' &&
    country.trim() !== '' &&
    prefixes.length > 0

  async function save() {
    setSaving(true)
    setError(null)
    try {
      if (editing) {
        await updateShippingArea(areaCode, { name, country, prefixes })
      } else {
        await createShippingArea({ code: areaCode, name, country, prefixes })
      }
      void navigate('/shipping-areas')
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mx-auto flex max-w-xl flex-col gap-6">
      <div>
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          {editing ? `Edit ${code}` : 'New shipping area'}
        </h1>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>{editing ? 'Area details' : 'Define the area'}</CardTitle>
          <CardDescription>
            Prefixes match the start of a postcode; paste them separated by commas,
            spaces, or new lines. They are stored uppercase and deduplicated.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="grid gap-1.5">
            <Label htmlFor="area-code">Code</Label>
            <Input
              id="area-code"
              value={areaCode}
              onChange={(event) => setAreaCode(event.target.value)}
              disabled={editing}
              placeholder="e.g. scottish-highlands"
            />
            {editing && (
              <p className="text-xs text-muted-foreground">
                Rulebook rules reference the code, so it cannot change.
              </p>
            )}
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="area-name">Name</Label>
            <Input
              id="area-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="e.g. Scottish Highlands"
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="area-country">Country</Label>
            <Input
              id="area-country"
              value={country}
              onChange={(event) => setCountry(event.target.value)}
              placeholder="e.g. GB"
              maxLength={3}
            />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="area-prefixes">Postcode prefixes</Label>
            <textarea
              id="area-prefixes"
              value={prefixText}
              onChange={(event) => setPrefixText(event.target.value)}
              rows={4}
              className="w-full rounded-md border bg-transparent px-3 py-2 text-sm"
              placeholder="AB, IV, KW…"
            />
            <p className="text-xs text-muted-foreground">
              {prefixes.length} prefix{prefixes.length === 1 ? '' : 'es'}
            </p>
          </div>
          {error !== null && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <div className="flex gap-2">
            <Button disabled={!canSave} onClick={() => void save()}>
              {saving ? 'Saving…' : 'Save'}
            </Button>
            <Button
              nativeButton={false}
              variant="outline"
              render={<Link to="/shipping-areas" />}
            >
              Back to list
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
