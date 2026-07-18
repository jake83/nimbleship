import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { createDraft, fetchActiveRulebook, fetchVersion } from '@/rulebook/api'
import {
  fromDeclaration,
  toDeclaration,
  validateDraft,
} from '@/rulebook/draft'
import type { EditableService, ServiceFieldErrors } from '@/rulebook/draft'

interface Row {
  key: number
  service: EditableService
}

const EMPTY_SERVICE: EditableService = {
  code: '',
  carrier: '',
  name: '',
  weightMinKg: '0',
  weightMaxKg: '',
  countries: '',
  cost: '',
  tieBreakOrder: '',
  maxDimensionCm: '',
  maxGirthCm: '',
  areasServed: '',
  areasBlocked: '',
  propositions: '',
  costBands: null,
  chargeBands: null,
}

interface FieldProps {
  id: string
  label: string
  value: string
  error?: string
  hint?: string
  onChange: (value: string) => void
}

function Field({ id, label, value, error, hint, onChange }: FieldProps) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        value={value}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? `${id}-error` : undefined}
        onChange={(event) => onChange(event.target.value)}
      />
      {error ? (
        <p id={`${id}-error`} className="text-xs text-destructive">
          {error}
        </p>
      ) : (
        hint && <p className="text-xs text-muted-foreground">{hint}</p>
      )}
    </div>
  )
}

function bandsNote(service: EditableService): string | null {
  const parts: string[] = []
  if (service.costBands?.length) {
    parts.push(`${service.costBands.length} cost band(s)`)
  }
  if (service.chargeBands?.length) {
    parts.push(`${service.chargeBands.length} charge band(s)`)
  }
  if (parts.length === 0) return null
  return `Has ${parts.join(' and ')}, managed elsewhere and carried through unchanged.`
}

interface ServiceCardProps {
  row: Row
  index: number
  errors: ServiceFieldErrors
  onChange: (service: EditableService) => void
  onRemove: () => void
}

function ServiceCard({ row, index, errors, onChange, onRemove }: ServiceCardProps) {
  const { service } = row
  const id = (field: string) => `service-${row.key}-${field}`
  const set = (field: keyof EditableService) => (value: string) =>
    onChange({ ...service, [field]: value })
  const note = bandsNote(service)

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {service.code.trim() === '' ? `Service ${index + 1}` : service.code}
        </CardTitle>
        {service.name.trim() !== '' && (
          <CardDescription>{service.name}</CardDescription>
        )}
        <CardAction>
          <Button type="button" variant="ghost" size="sm" onClick={onRemove}>
            Remove service
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Field
          id={id('code')}
          label="Code"
          value={service.code}
          error={errors.code}
          onChange={set('code')}
        />
        <Field
          id={id('carrier')}
          label="Carrier"
          value={service.carrier}
          error={errors.carrier}
          onChange={set('carrier')}
        />
        <Field
          id={id('name')}
          label="Name"
          value={service.name}
          error={errors.name}
          onChange={set('name')}
        />
        <Field
          id={id('weight-min')}
          label="Weight min (kg)"
          value={service.weightMinKg}
          error={errors.weightMinKg}
          onChange={set('weightMinKg')}
        />
        <Field
          id={id('weight-max')}
          label="Weight max (kg)"
          value={service.weightMaxKg}
          error={errors.weightMaxKg}
          onChange={set('weightMaxKg')}
        />
        <Field
          id={id('cost')}
          label="Cost"
          value={service.cost}
          error={errors.cost}
          hint="Delivery Cost: what the carrier charges, used to pick the cheapest."
          onChange={set('cost')}
        />
        <Field
          id={id('countries')}
          label="Countries"
          value={service.countries}
          error={errors.countries}
          hint="Comma-separated country codes, e.g. GB, IE."
          onChange={set('countries')}
        />
        <Field
          id={id('tie-break')}
          label="Tie-break order"
          value={service.tieBreakOrder}
          error={errors.tieBreakOrder}
          hint="Breaks cost ties; must be unique across services."
          onChange={set('tieBreakOrder')}
        />
        <Field
          id={id('max-dimension')}
          label="Max dimension (cm)"
          value={service.maxDimensionCm}
          error={errors.maxDimensionCm}
          hint="Blank = no limit."
          onChange={set('maxDimensionCm')}
        />
        <Field
          id={id('max-girth')}
          label="Max girth (cm)"
          value={service.maxGirthCm}
          error={errors.maxGirthCm}
          hint="Blank = no limit."
          onChange={set('maxGirthCm')}
        />
        <Field
          id={id('areas-served')}
          label="Areas served"
          value={service.areasServed}
          hint="Shipping Area codes; blank = anywhere in the allowed countries."
          onChange={set('areasServed')}
        />
        <Field
          id={id('areas-blocked')}
          label="Areas blocked"
          value={service.areasBlocked}
          hint="Shipping Area codes this service never delivers to."
          onChange={set('areasBlocked')}
        />
        <Field
          id={id('propositions')}
          label="Delivery Propositions"
          value={service.propositions}
          hint="Proposition codes this service fulfils; blank = unrestricted."
          onChange={set('propositions')}
        />
        {note && (
          <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-3">
            {note}
          </p>
        )}
      </CardContent>
    </Card>
  )
}

/**
 * Form-based authoring of a new draft version (ADR 0003): seeded from an
 * existing version, validated as you type, saved as an immutable draft.
 */
export function DraftEditorPage() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const from = searchParams.get('from')

  const nextKey = useRef(0)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [seedLabel, setSeedLabel] = useState<string | null>(null)
  const [author, setAuthor] = useState('')
  const [description, setDescription] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    const seed =
      from !== null
        ? fetchVersion(Number(from)).then((detail) => ({
            label: `version ${detail.version}`,
            services: detail.services,
          }))
        : fetchActiveRulebook().then((active) => ({
            label: `version ${active.version} (live)`,
            services: active.services,
          }))
    seed
      .then(({ label, services }) => {
        if (cancelled) return
        setSeedLabel(label)
        setRows(
          services.map((service) => ({
            key: nextKey.current++,
            service: fromDeclaration(service),
          })),
        )
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setLoadError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [from])

  if (loadError !== null) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {loadError}
      </p>
    )
  }
  if (rows === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  const validation = validateDraft(rows.map((row) => row.service))
  const authorError =
    author.trim() === ''
      ? 'Required'
      : author.trim().length > 64
        ? 'Must be 64 characters or fewer'
        : undefined
  const canSave = validation.valid && !authorError && !saving

  async function save() {
    setSaving(true)
    setSaveError(null)
    try {
      const created = await createDraft(
        rows!.map((row) => toDeclaration(row.service)),
        author.trim(),
        description.trim() === '' ? null : description.trim(),
      )
      navigate(`/rulebook/versions/${created.version}`)
    } catch (caught) {
      setSaveError(caught instanceof Error ? caught.message : String(caught))
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            New draft
          </h1>
          <p className="text-sm text-muted-foreground">
            Started from {seedLabel}. Saving creates a new immutable draft
            version to dry-run and publish.
          </p>
        </div>
        <Button nativeButton={false} variant="ghost" render={<Link to="/rulebook" />}>
          Back to versions
        </Button>
      </div>

      {validation.draftErrors.map((message) => (
        <p key={message} role="alert" className="text-sm text-destructive">
          {message}
        </p>
      ))}

      <div className="flex flex-col gap-4">
        {rows.map((row, index) => (
          <ServiceCard
            key={row.key}
            row={row}
            index={index}
            errors={validation.serviceErrors[index] ?? {}}
            onChange={(service) =>
              setRows(
                rows.map((other) =>
                  other.key === row.key ? { ...other, service } : other,
                ),
              )
            }
            onRemove={() =>
              setRows(rows.filter((other) => other.key !== row.key))
            }
          />
        ))}
      </div>

      <div>
        <Button
          type="button"
          variant="outline"
          onClick={() =>
            setRows([
              ...rows,
              { key: nextKey.current++, service: { ...EMPTY_SERVICE } },
            ])
          }
        >
          Add service
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Save draft</CardTitle>
          <CardDescription>
            The draft becomes version history immediately; it affects nothing
            until published.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex max-w-xs flex-col gap-1.5">
            <Label htmlFor="draft-author">Author</Label>
            <Input
              id="draft-author"
              value={author}
              aria-invalid={author !== '' && authorError ? true : undefined}
              onChange={(event) => setAuthor(event.target.value)}
            />
            {author !== '' && authorError && (
              <p className="text-xs text-destructive">{authorError}</p>
            )}
          </div>
          <div className="flex max-w-md flex-col gap-1.5">
            <Label htmlFor="draft-description">Description (optional)</Label>
            <Input
              id="draft-description"
              value={description}
              maxLength={280}
              placeholder="Why this version exists"
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>
          {saveError !== null && (
            <p role="alert" className="text-sm text-destructive">
              {saveError}
            </p>
          )}
          <div>
            <Button type="button" onClick={save} disabled={!canSave}>
              {saving ? 'Saving...' : 'Save draft'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
