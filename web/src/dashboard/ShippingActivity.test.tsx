import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { stubFetch } from '@/test/rulebook'

import { ShippingActivity } from './ShippingActivity'

afterEach(() => {
  vi.unstubAllGlobals()
})

const SEVEN_DAY = {
  range: '7d',
  kpis: {
    consignments_today: 42,
    consignments_yesterday: 38,
    failures_today: 3,
    success_rate_7d: 97.5,
    busiest_carrier_7d: { carrier: 'acme', count: 120 },
  },
  buckets: ['2026-07-14', '2026-07-15'],
  volume: [
    { carrier: 'acme', data: [10, 12] },
    { carrier: 'other', data: [2, 1] },
  ],
  success_failure: { success: [11, 12], failed: [1, 1] },
  manifest_queue: { pending: 2, failed: 1, sent_today: 5 },
}

const TODAY = {
  ...SEVEN_DAY,
  range: 'today',
  buckets: ['00:00', '01:00'],
}

describe('ShippingActivity', () => {
  it('shows the KPIs and the manifest queue for the default range', async () => {
    stubFetch({
      'GET /api/dashboard/shipping-stats?range=7d': { body: SEVEN_DAY },
    })
    render(<ShippingActivity />)

    expect(await screen.findByText('42')).toBeInTheDocument()
    expect(screen.getByText(/consignments today/i)).toBeInTheDocument()
    expect(screen.getByText('97.5%')).toBeInTheDocument()
    expect(screen.getByText(/acme · 120/)).toBeInTheDocument()
    // The failure queue is the actionable number, badged when non-zero.
    expect(screen.getByText(/1 failed/i)).toBeInTheDocument()
    expect(screen.getByText(/2 pending/i)).toBeInTheDocument()
  })

  it('switches ranges and refetches', async () => {
    stubFetch({
      'GET /api/dashboard/shipping-stats?range=7d': { body: SEVEN_DAY },
      'GET /api/dashboard/shipping-stats?range=today': { body: TODAY },
    })
    render(<ShippingActivity />)
    await screen.findByText('42')

    await userEvent.click(screen.getByRole('button', { name: /^today$/i }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /^today$/i })).toHaveAttribute(
        'aria-pressed',
        'true',
      ),
    )
  })

  it('keeps the failure chart when calls exist but no consignments do', async () => {
    // A retrying manifest makes carrier calls on a morning with no new orders;
    // the failure chart must not vanish behind "no shipments" (refuter, PR #139).
    stubFetch({
      'GET /api/dashboard/shipping-stats?range=7d': {
        body: {
          ...SEVEN_DAY,
          volume: [],
          success_failure: { success: [0, 2], failed: [0, 2] },
        },
      },
    })
    render(<ShippingActivity />)

    expect(await screen.findByText(/no shipments in this period/i)).toBeInTheDocument()
    expect(screen.getByText(/success vs failure/i)).toBeInTheDocument()
    expect(
      screen.queryByText(/no carrier calls in this period/i),
    ).not.toBeInTheDocument()
  })

  it('reads as quiet, not broken, on a fresh install', async () => {
    stubFetch({
      'GET /api/dashboard/shipping-stats?range=7d': {
        body: {
          range: '7d',
          kpis: {
            consignments_today: 0,
            consignments_yesterday: 0,
            failures_today: 0,
            success_rate_7d: null,
            busiest_carrier_7d: null,
          },
          buckets: ['2026-07-14'],
          volume: [],
          success_failure: { success: [0], failed: [0] },
          manifest_queue: { pending: 0, failed: 0, sent_today: 0 },
        },
      },
    })
    render(<ShippingActivity />)

    expect(await screen.findByText(/no shipments in this period/i)).toBeInTheDocument()
    expect(screen.getByText(/no carrier calls in this period/i)).toBeInTheDocument()
    expect(screen.getAllByText('–').length).toBeGreaterThan(0) // absent rate, not 0%
  })
})
