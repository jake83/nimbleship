import { useEffect, useMemo, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Line, LineChart, XAxis } from 'recharts'

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
  ChartContainer,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from '@/components/ui/chart'
import { fetchShippingStats, type ShippingStats, type StatsRange } from './api'

const RANGES: { value: StatsRange; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7d' },
  { value: '1m', label: '1m' },
]

// Cycle chart theme slots; carriers are data, so colours cannot be per-carrier
// constants.
const SLOT_COLOURS = [
  'var(--chart-1)',
  'var(--chart-2)',
  'var(--chart-3)',
  'var(--chart-4)',
  'var(--chart-5)',
]

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold tracking-tight">{value}</p>
    </div>
  )
}

/** The home page's shipping activity: KPIs, volume by carrier, success vs
 * failure, and the manifest queue - the day's health at a glance. */
export function ShippingActivity() {
  const [range, setRange] = useState<StatsRange>('7d')
  const [stats, setStats] = useState<ShippingStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setError(null)
    fetchShippingStats(range)
      .then((body) => {
        if (!cancelled) setStats(body)
      })
      .catch((caught: unknown) => {
        if (!cancelled)
          setError(caught instanceof Error ? caught.message : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [range])

  const volumeRows = useMemo(() => {
    if (stats === null) return []
    return stats.buckets.map((bucket, index) => {
      const row: Record<string, string | number> = { bucket }
      for (const series of stats.volume) {
        row[series.carrier] = series.data[index] ?? 0
      }
      return row
    })
  }, [stats])

  const outcomeRows = useMemo(() => {
    if (stats === null) return []
    return stats.buckets.map((bucket, index) => ({
      bucket,
      success: stats.success_failure.success[index] ?? 0,
      failed: stats.success_failure.failed[index] ?? 0,
    }))
  }, [stats])

  if (error !== null) {
    return (
      <p role="alert" className="text-sm text-destructive">
        {error}
      </p>
    )
  }
  if (stats === null) {
    return <p className="text-sm text-muted-foreground">Loading activity...</p>
  }

  const { kpis, manifest_queue: queue } = stats
  // Volume (consignments) and outcomes (carrier calls) are independent series:
  // a retrying manifest makes calls on a morning with no new orders, so each
  // chart goes quiet on its own - one shared flag would claim "no shipments"
  // right under a live failure badge.
  const noVolume = stats.volume.length === 0
  const noCalls =
    stats.success_failure.success.every((count) => count === 0) &&
    stats.success_failure.failed.every((count) => count === 0)
  const volumeConfig: ChartConfig = Object.fromEntries(
    stats.volume.map((series, index) => [
      series.carrier,
      {
        label: series.carrier,
        color: SLOT_COLOURS[index % SLOT_COLOURS.length],
      },
    ]),
  )
  const outcomeConfig: ChartConfig = {
    success: { label: 'success', color: 'var(--chart-2)' },
    // Failures share the destructive colour with the queue badges, not a grey.
    failed: { label: 'failed', color: 'var(--destructive)' },
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-heading text-lg font-semibold tracking-tight">
          Shipping activity
        </h2>
        <div className="flex gap-1">
          {RANGES.map((option) => (
            <Button
              key={option.value}
              size="sm"
              variant={option.value === range ? 'secondary' : 'ghost'}
              aria-pressed={option.value === range}
              onClick={() => setRange(option.value)}
            >
              {option.label}
            </Button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Kpi label="Consignments today" value={String(kpis.consignments_today)} />
        <Kpi label="Yesterday" value={String(kpis.consignments_yesterday)} />
        <Kpi
          label="Success rate (7d)"
          value={
            kpis.success_rate_7d !== null ? `${kpis.success_rate_7d}%` : '–'
          }
        />
        <Kpi
          label="Busiest carrier (7d)"
          value={
            kpis.busiest_carrier_7d !== null
              ? `${kpis.busiest_carrier_7d.carrier} · ${kpis.busiest_carrier_7d.count}`
              : '–'
          }
        />
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Manifest queue:</span>
        <Badge variant={queue.failed > 0 ? 'destructive' : 'outline'}>
          {queue.failed} failed
        </Badge>
        <Badge variant="outline">{queue.pending} pending</Badge>
        <Badge variant="outline">{queue.sent_today} sent today</Badge>
        {kpis.failures_today > 0 && (
          <Badge variant="destructive">
            {kpis.failures_today} failed carrier calls today
          </Badge>
        )}
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Volume by carrier</CardTitle>
            <CardDescription>Consignments created</CardDescription>
          </CardHeader>
          <CardContent>
            {noVolume ? (
              <p className="text-sm text-muted-foreground">
                No shipments in this period.
              </p>
            ) : (
              <ChartContainer config={volumeConfig} className="h-48 w-full">
                <BarChart data={volumeRows}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="bucket" tickLine={false} axisLine={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  {stats.volume.map((series) => (
                    <Bar
                      key={series.carrier}
                      dataKey={series.carrier}
                      stackId="volume"
                      fill={`var(--color-${series.carrier})`}
                    />
                  ))}
                </BarChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Success vs failure</CardTitle>
            <CardDescription>Carrier calls</CardDescription>
          </CardHeader>
          <CardContent>
            {noCalls ? (
              <p className="text-sm text-muted-foreground">
                No carrier calls in this period.
              </p>
            ) : (
              <ChartContainer config={outcomeConfig} className="h-48 w-full">
                <LineChart data={outcomeRows}>
                  <CartesianGrid vertical={false} />
                  <XAxis dataKey="bucket" tickLine={false} axisLine={false} />
                  <ChartTooltip content={<ChartTooltipContent />} />
                  {/* The line-draw animation can stick at frame zero (dashoffset
                      never transitions), leaving the series invisible - render
                      statically. */}
                  <Line
                    dataKey="success"
                    stroke="var(--color-success)"
                    dot={false}
                    isAnimationActive={false}
                  />
                  <Line
                    dataKey="failed"
                    stroke="var(--color-failed)"
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ChartContainer>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
