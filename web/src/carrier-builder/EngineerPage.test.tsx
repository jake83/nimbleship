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
