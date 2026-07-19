import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

import { sentBody, stubFetch } from '@/test/rulebook'

import { EngineerPage } from './EngineerPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const OPEN_BLOCKER = {
  id: 7,
  carrier: 'acme',
  kind: 'needs_plugin',
  title: 'HMAC signing',
  detail: 'Requests need an HMAC signature; the engine has no plugin.',
  plugin_name: 'acme_hmac',
  status: 'open',
  resolution: null,
  created_at: '2026-07-19T10:00:00Z',
  resolved_at: null,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <EngineerPage />
    </MemoryRouter>,
  )
}

describe('EngineerPage', () => {
  it('loads a carrier queue and resolves a blocker with a recorded answer', async () => {
    const mock = stubFetch({
      'GET /api/carrier-builder/blockers?carrier=acme': { body: [OPEN_BLOCKER] },
      'POST /api/carrier-builder/blockers/7/resolve': {
        body: {
          ...OPEN_BLOCKER,
          status: 'resolved',
          resolution: 'Shipped as acme_hmac in v1.42.',
        },
      },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acme')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))

    expect(await screen.findByText('HMAC signing')).toBeInTheDocument()
    expect(screen.getByText(/needs a plugin: acme_hmac/i)).toBeInTheDocument()

    await userEvent.type(
      screen.getByLabelText(/resolution/i),
      'Shipped as acme_hmac in v1.42.',
    )
    await userEvent.click(screen.getByRole('button', { name: /^resolve$/i }))

    expect(
      await screen.findByText(/resolved: shipped as acme_hmac in v1.42/i),
    ).toBeInTheDocument()
    expect(sentBody(mock, 'POST /api/carrier-builder/blockers/7/resolve')).toEqual(
      { resolution: 'Shipped as acme_hmac in v1.42.' },
    )
  })

  it('a slow first load does not clobber a faster second load', async () => {
    // Retype the carrier and re-load before the first response lands: only the
    // latest request may apply, or the engineer sees the wrong carrier's queue
    // under the right carrier's name.
    let resolveFirst: (value: Response) => void = () => {}
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input)
        if (url.endsWith('carrier=acm'))
          return new Promise<Response>((resolve) => {
            resolveFirst = resolve
          })
        if (url.endsWith('carrier=acme')) return json([OPEN_BLOCKER])
        throw new Error(`unmocked fetch: ${url}`)
      }),
    )
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acm')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    await userEvent.type(screen.getByLabelText('Carrier'), 'e') // now "acme"
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    expect(await screen.findByText('HMAC signing')).toBeInTheDocument()

    // The stale first response lands last - it must not replace acme's queue.
    resolveFirst(
      json([{ ...OPEN_BLOCKER, id: 9, carrier: 'acm', title: 'Wrong queue' }]),
    )
    expect(await screen.findByText('HMAC signing')).toBeInTheDocument()
    expect(screen.queryByText('Wrong queue')).not.toBeInTheDocument()
  })

  it('describes a needs_decision blocker as needing a decision', async () => {
    stubFetch({
      'GET /api/carrier-builder/blockers?carrier=acme': {
        body: [
          {
            ...OPEN_BLOCKER,
            kind: 'needs_decision',
            plugin_name: null,
            title: 'Which endpoint?',
          },
        ],
      },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acme')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))

    expect(await screen.findByText('Which endpoint?')).toBeInTheDocument()
    expect(screen.getByText(/needs a decision/i)).toBeInTheDocument()
  })

  it('a hung resolve locks only its own button, not the whole queue', async () => {
    const second = {
      ...OPEN_BLOCKER,
      id: 8,
      kind: 'needs_decision',
      plugin_name: null,
      title: 'Which endpoint?',
    }
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const key = `${init?.method ?? 'GET'} ${String(input)}`
        if (key === 'GET /api/carrier-builder/blockers?carrier=acme')
          return json([OPEN_BLOCKER, second])
        if (key === 'POST /api/carrier-builder/blockers/7/resolve')
          return new Promise<Response>(() => {}) // hangs
        if (key === 'POST /api/carrier-builder/blockers/8/resolve')
          return json({ ...second, status: 'resolved', resolution: 'use live' })
        throw new Error(`unmocked fetch: ${key}`)
      }),
    )
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acme')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    await screen.findByText('HMAC signing')
    const [first, next] = screen.getAllByLabelText(/resolution/i)
    await userEvent.type(first!, 'shipping it')
    await userEvent.type(next!, 'use live')

    const buttons = screen.getAllByRole('button', { name: /^resolve$/i })
    await userEvent.click(buttons[0]!)

    // The first blocker's button is busy; the second stays workable.
    expect(buttons[0]).toBeDisabled()
    expect(buttons[1]).toBeEnabled()

    // The second blocker resolving must not free the first's still-in-flight
    // button (a scalar busy id would - the double-submit the guard exists for).
    await userEvent.click(buttons[1]!)
    await screen.findByText(/resolved: use live/i)
    expect(buttons[0]).toBeDisabled()
  })

  it('does not attribute a stale resolve failure to a newly loaded carrier', async () => {
    let rejectResolve: (value: Response) => void = () => {}
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const key = `${init?.method ?? 'GET'} ${String(input)}`
        if (key === 'GET /api/carrier-builder/blockers?carrier=acme')
          return json([OPEN_BLOCKER])
        if (key === 'GET /api/carrier-builder/blockers?carrier=globex')
          return json([])
        if (key === 'POST /api/carrier-builder/blockers/7/resolve')
          return new Promise<Response>((resolve) => {
            rejectResolve = resolve
          })
        throw new Error(`unmocked fetch: ${key}`)
      }),
    )
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acme')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    await screen.findByText('HMAC signing')
    await userEvent.type(screen.getByLabelText(/resolution/i), 'mine')
    await userEvent.click(screen.getByRole('button', { name: /^resolve$/i }))

    // The engineer moves to another carrier while the resolve is in flight...
    await userEvent.clear(screen.getByLabelText('Carrier'))
    await userEvent.type(screen.getByLabelText('Carrier'), 'globex')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    await screen.findByText(/no handoffs for this carrier/i)

    // ...then the stale resolve fails: the error must not appear under globex.
    rejectResolve(json({ detail: 'blocker 7 is already resolved' }, 409))
    await new Promise((settle) => setTimeout(settle, 0))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows an empty state when the carrier has no handoffs', async () => {
    stubFetch({
      'GET /api/carrier-builder/blockers?carrier=quiet': { body: [] },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'quiet')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))

    expect(
      await screen.findByText(/no handoffs for this carrier/i),
    ).toBeInTheDocument()
  })

  it('surfaces a conflict when the blocker was already resolved', async () => {
    stubFetch({
      'GET /api/carrier-builder/blockers?carrier=acme': { body: [OPEN_BLOCKER] },
      'POST /api/carrier-builder/blockers/7/resolve': {
        body: { detail: 'blocker 7 is already resolved' },
        status: 409,
      },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier'), 'acme')
    await userEvent.click(screen.getByRole('button', { name: /load handoffs/i }))
    await screen.findByText('HMAC signing')
    await userEvent.type(screen.getByLabelText(/resolution/i), 'mine')
    await userEvent.click(screen.getByRole('button', { name: /^resolve$/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/already resolved/i)
  })
})
