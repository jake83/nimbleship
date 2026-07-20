import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { fetchCarriers, type CarrierSummary } from '@/carriers/api'

/** The carriers catalog: every carrier known to the install (published, drafted,
 * or config-only), each linking to its config surface. */
export function CarriersPage() {
  const [carriers, setCarriers] = useState<CarrierSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchCarriers()
      .then((list) => {
        if (!cancelled) setCarriers(list)
      })
      .catch((caught: unknown) => {
        if (!cancelled)
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
  if (carriers === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="font-heading text-2xl font-semibold tracking-tight">
          Carriers
        </h1>
        <p className="text-sm text-muted-foreground">
          Every carrier this install knows: live, drafted, or credentials-only.
        </p>
      </div>
      {carriers.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No carriers yet - onboard one with the carrier builder.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Carrier</TableHead>
              <TableHead>Definition</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {carriers.map((row) => (
              <TableRow key={row.carrier}>
                <TableCell className="font-mono text-xs">{row.carrier}</TableCell>
                <TableCell>
                  {row.active_version !== null ? (
                    <Badge>v{row.active_version} live</Badge>
                  ) : (
                    <Badge variant="outline">no published definition</Badge>
                  )}
                </TableCell>
                <TableCell>
                  <Button
                    nativeButton={false}
                    variant="outline"
                    size="sm"
                    render={<Link to={`/carriers/${row.carrier}/config`} />}
                  >
                    Config
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}
