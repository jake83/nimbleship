import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
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
import { fetchShippingAreas, type ShippingArea } from '@/shipping-areas/api'

const PREVIEW_PREFIXES = 5

function matches(area: ShippingArea, needle: string): boolean {
  const haystack = needle.trim().toUpperCase()
  if (haystack === '') return true
  return (
    area.name.toUpperCase().includes(haystack) ||
    area.code.toUpperCase().includes(haystack) ||
    area.prefixes.some((prefix) => prefix.startsWith(haystack))
  )
}

function PrefixCell({ prefixes }: { prefixes: string[] }) {
  const [expanded, setExpanded] = useState(false)
  if (expanded || prefixes.length <= PREVIEW_PREFIXES) {
    return <span>{prefixes.join(', ')}</span>
  }
  return (
    <span className="flex items-center gap-2">
      <span>
        {prefixes.slice(0, PREVIEW_PREFIXES).join(', ')} +
        {prefixes.length - PREVIEW_PREFIXES} more
      </span>
      <Button
        variant="ghost"
        size="sm"
        aria-label={`Show all ${prefixes.length} prefixes`}
        onClick={() => setExpanded(true)}
      >
        Show all
      </Button>
    </span>
  )
}

/** The Shipping Areas admin: named geographies and the postcode prefixes that
 * define them. Rulebook rules reference areas by code, which is why there is no
 * delete here - removal needs a story for the rules that still point at one. */
export function ShippingAreasPage() {
  const [areas, setAreas] = useState<ShippingArea[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState('')

  useEffect(() => {
    let cancelled = false
    fetchShippingAreas()
      .then((list) => {
        if (!cancelled) setAreas(list)
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
  if (areas === null) {
    return <p className="text-sm text-muted-foreground">Loading...</p>
  }

  const visible = areas.filter((area) => matches(area, search))

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            Shipping areas
          </h1>
          <p className="text-sm text-muted-foreground">
            Named geographies the rulebook can serve or block, defined by postcode
            prefixes.
          </p>
        </div>
        <Button
          nativeButton={false}
          render={<Link to="/shipping-areas/new" />}
        >
          New shipping area
        </Button>
      </div>

      <div className="grid max-w-xs gap-1.5">
        <Label htmlFor="area-search">Search</Label>
        <Input
          id="area-search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Name, code, or postcode prefix"
        />
      </div>

      {visible.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {areas.length === 0
            ? 'No shipping areas yet.'
            : 'No areas match the search.'}
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Code</TableHead>
              <TableHead>Name</TableHead>
              <TableHead>Country</TableHead>
              <TableHead>Prefixes</TableHead>
              <TableHead />
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((area) => (
              <TableRow key={area.code}>
                <TableCell className="font-mono text-xs">{area.code}</TableCell>
                <TableCell>{area.name}</TableCell>
                <TableCell>{area.country}</TableCell>
                <TableCell className="text-muted-foreground">
                  <PrefixCell prefixes={area.prefixes} />
                </TableCell>
                <TableCell>
                  <Button
                    nativeButton={false}
                    variant="outline"
                    size="sm"
                    render={<Link to={`/shipping-areas/${area.code}`} />}
                  >
                    Edit
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
