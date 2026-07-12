import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fetchActiveRulebook, fetchVersion, publishVersion } from '@/rulebook/api'
import { diffServices, formatValue } from '@/rulebook/diff'
import { dryRunSummary } from '@/rulebook/dry-run'
import { DryRunPanel } from '@/rulebook/DryRunPanel'
import type {
  DryRunOutcome,
  ServiceDeclaration,
  VersionDetail,
} from '@/rulebook/types'

export function StatusBadge({ status, live }: { status: string; live: boolean }) {
  return (
    <span className="inline-flex gap-1">
      <Badge variant={status === 'draft' ? 'secondary' : 'default'}>
        {status}
      </Badge>
      {live && <Badge variant="destructive">live</Badge>}
    </span>
  )
}

function ServicesTable({ services }: { services: ServiceDeclaration[] }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Code</TableHead>
          <TableHead>Carrier</TableHead>
          <TableHead>Name</TableHead>
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
            <TableCell>{service.name}</TableCell>
            <TableCell>
              {service.weight_min_kg} to {service.weight_max_kg}
            </TableCell>
            <TableCell>{formatValue(service.countries)}</TableCell>
            <TableCell>{service.cost}</TableCell>
            <TableCell>{service.tie_break_order}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function DiffView({
  previous,
  current,
}: {
  previous: VersionDetail
  current: VersionDetail
}) {
  const diff = diffServices(previous.services, current.services)
  return (
    <section
      aria-label={`Changes from version ${previous.version}`}
      className="flex flex-col gap-2"
    >
      <h2 className="font-heading text-lg font-medium">
        Changes from version {previous.version}
      </h2>
      {diff.entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No changes: identical to version {previous.version}.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {diff.entries.map((entry) => (
            <li key={`${entry.kind}-${entry.code}`} className="text-sm">
              <span className="inline-flex items-center gap-2">
                {entry.kind === 'added' && <Badge>added</Badge>}
                {entry.kind === 'removed' && (
                  <Badge variant="destructive">removed</Badge>
                )}
                {entry.kind === 'changed' && (
                  <Badge variant="secondary">changed</Badge>
                )}
                <span className="font-mono font-medium">{entry.code}</span>
              </span>
              {entry.kind === 'changed' && (
                <ul className="mt-1 ml-4 flex list-disc flex-col gap-0.5 text-muted-foreground">
                  {entry.changes.map((change) => (
                    <li key={change.field}>
                      <span className="font-medium text-foreground">
                        {change.field}
                      </span>
                      : {change.before}{' '}
                      <span aria-hidden>-&gt;</span>
                      <span className="sr-only">changed to</span>{' '}
                      {change.after}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted-foreground">
        {diff.unchangedCount} service(s) unchanged.
      </p>
    </section>
  )
}

function PublishDialog({
  version,
  outcome,
  onPublished,
}: {
  version: number
  outcome: DryRunOutcome | null
  onPublished: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [publishing, setPublishing] = useState(false)

  async function confirm() {
    setPublishing(true)
    setError(null)
    try {
      await publishVersion(version)
      onPublished()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setPublishing(false)
    }
  }

  return (
    <AlertDialog>
      <AlertDialogTrigger render={<Button />}>Publish...</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Publish version {version}?</AlertDialogTitle>
          <AlertDialogDescription>
            {outcome
              ? `Dry run of ${outcome.total} historical orders: ${dryRunSummary(outcome)}`
              : 'No dry run has been executed against this draft. Consider running one below before publishing.'}{' '}
            Publishing makes this version live for all new allocations;
            rolling back means drafting and publishing a new version.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {error !== null && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <Button type="button" onClick={confirm} disabled={publishing}>
            {publishing ? 'Publishing...' : `Publish version ${version}`}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** One rulebook version: its declarations, its diff, its test-and-publish rail. */
export function VersionPage() {
  const params = useParams()
  const versionNumber = Number(params.version)

  const [detail, setDetail] = useState<VersionDetail | null>(null)
  const [previous, setPrevious] = useState<VersionDetail | null>(null)
  const [liveVersion, setLiveVersion] = useState<number | null>(null)
  const [outcome, setOutcome] = useState<DryRunOutcome | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    // Versions are sequential and immutable, so version - 1 always exists
    // for version > 1; the seed (version 1) has nothing to diff against.
    Promise.all([
      fetchVersion(versionNumber),
      versionNumber > 1 ? fetchVersion(versionNumber - 1) : null,
      fetchActiveRulebook(),
    ])
      .then(([current, prior, active]) => {
        if (cancelled) return
        setDetail(current)
        setPrevious(prior)
        setLiveVersion(active.version)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [versionNumber])

  if (error !== null) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    )
  }
  if (detail === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="flex items-center gap-3 font-heading text-2xl font-semibold tracking-tight">
            Version {detail.version}
            <StatusBadge
              status={detail.status}
              live={detail.version === liveVersion}
            />
          </h1>
          <p className="text-sm text-muted-foreground">
            By {detail.author} on {new Date(detail.created_at).toLocaleString()}
          </p>
        </div>
        <div className="flex gap-2">
          <Button nativeButton={false} variant="ghost" render={<Link to="/rulebook" />}>
            Back to versions
          </Button>
          <Button
            nativeButton={false}
            variant="outline"
            render={<Link to={`/rulebook/drafts/new?from=${detail.version}`} />}
          >
            Edit as new draft
          </Button>
          {detail.status === 'draft' && (
            <PublishDialog
              version={detail.version}
              outcome={outcome}
              onPublished={() =>
                setDetail({ ...detail, status: 'published' })
              }
            />
          )}
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Service declarations</CardTitle>
          <CardDescription>
            The carrier services this version declares, matched automatically
            against shipment facts.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ServicesTable services={detail.services} />
        </CardContent>
      </Card>

      {previous !== null ? (
        <DiffView previous={previous} current={detail} />
      ) : (
        <p className="text-sm text-muted-foreground">
          Initial version: nothing earlier to compare against.
        </p>
      )}

      <DryRunPanel version={detail.version} onOutcome={setOutcome} />
    </div>
  )
}
