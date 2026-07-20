/** The dashboard's shipping-stats read, aggregated server-side from what the
 * domain already records - no client-side derivation beyond display. */

export type StatsRange = 'today' | '7d' | '1m'

export interface ShippingStats {
  range: StatsRange
  kpis: {
    consignments_today: number
    consignments_yesterday: number
    failures_today: number
    /** null when the window saw no carrier calls: an absent rate, not 0%. */
    success_rate_7d: number | null
    busiest_carrier_7d: { carrier: string; count: number } | null
  }
  buckets: string[]
  volume: { carrier: string; data: number[] }[]
  success_failure: { success: number[]; failed: number[] }
  manifest_queue: { pending: number; failed: number; sent_today: number }
}

export async function fetchShippingStats(
  range: StatsRange,
): Promise<ShippingStats> {
  const response = await fetch(`/api/dashboard/shipping-stats?range=${range}`)
  if (!response.ok) {
    throw new Error(`shipping stats failed (${response.status})`)
  }
  return (await response.json()) as ShippingStats
}
