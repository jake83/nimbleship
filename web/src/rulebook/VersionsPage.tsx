import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

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
import { fetchActiveRulebook, fetchVersions } from '@/rulebook/api'
import type { VersionSummary } from '@/rulebook/types'
import { StatusBadge } from '@/rulebook/VersionPage'

/** The rulebook's history: every immutable version, the live one flagged. */
export function VersionsPage() {
  const [versions, setVersions] = useState<VersionSummary[] | null>(null)
  const [liveVersion, setLiveVersion] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([fetchVersions(), fetchActiveRulebook()])
      .then(([list, active]) => {
        if (cancelled) return
        setVersions([...list].sort((a, b) => b.version - a.version))
        setLiveVersion(active.version)
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (error !== null) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    )
  }
  if (versions === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            Rulebook
          </h1>
          <p className="text-sm text-muted-foreground">
            Every version is immutable: draft, dry-run, publish. The highest
            published version is live.
          </p>
        </div>
        {liveVersion !== null && (
          <div className="flex gap-2">
            <Button
              nativeButton={false}
              variant="outline"
              render={<Link to="/rulebook/builder" />}
            >
              Build with AI
            </Button>
            <Button
              nativeButton={false}
              render={<Link to={`/rulebook/drafts/new?from=${liveVersion}`} />}
            >
              New draft
            </Button>
          </div>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Versions</CardTitle>
          <CardDescription>Newest first.</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Version</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Author</TableHead>
                <TableHead>Note</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {versions.map((version) => (
                <TableRow key={version.version}>
                  <TableCell>
                    <Link
                      to={`/rulebook/versions/${version.version}`}
                      className="font-medium text-primary underline-offset-4 hover:underline"
                    >
                      Version {version.version}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <StatusBadge
                      status={version.status}
                      live={version.version === liveVersion}
                    />
                  </TableCell>
                  <TableCell>{version.author}</TableCell>
                  <TableCell className="max-w-xs">
                    {version.description !== null ? (
                      <span
                        className="block truncate text-muted-foreground"
                        title={version.description}
                      >
                        {version.description}
                      </span>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {new Date(version.created_at).toLocaleString()}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
