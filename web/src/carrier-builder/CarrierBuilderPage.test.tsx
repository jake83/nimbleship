import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

import { sentBody, stubFetch } from '@/test/rulebook'

import { CarrierBuilderPage } from './CarrierBuilderPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPage() {
  return render(
    <MemoryRouter>
      <CarrierBuilderPage />
    </MemoryRouter>,
  )
}

// Mirrors the backend's own realistic fixture: an operation the real /check endpoint
// could actually report valid (an empty steps list could not).
const DRAFTED = {
  carrier: 'acme',
  name: 'Acme',
  auth: { scheme: 'none' },
  operations: {
    book: {
      steps: [
        {
          name: 'book',
          transport: 'http',
          request: {
            method: 'POST',
            url: 'config.url',
            content_type: 'json',
            mapping: [{ target: 'order', source: 'shipment.order_number' }],
          },
        },
      ],
    },
  },
}

describe('CarrierBuilderPage', () => {
  it('shows the capability board updating as the builder drafts', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: { reply: 'Drafted the booking call.', definition: DRAFTED },
      },
      'POST /api/carrier-builder/check': { body: { valid: true, errors: [] } },
    })
    renderPage()

    // Before any turn, nothing is drafted.
    expect(screen.getAllByText('not started').length).toBeGreaterThan(0)

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')

    expect(await screen.findByText(/drafted the booking call/i)).toBeInTheDocument()
    expect(screen.getByText(/Acme \(acme\)/)).toBeInTheDocument()
    expect(screen.getByText('Operation: book')).toBeInTheDocument()
    expect(
      await screen.findByText(/complete and ready to save/i),
    ).toBeInTheDocument()
  })

  it('lists what is still needed from the check outcome', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: {
          reply: 'Started.',
          definition: { carrier: 'acme', name: 'Acme' },
        },
      },
      'POST /api/carrier-builder/check': {
        body: { valid: false, errors: ['auth: Field required'] },
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')

    expect(await screen.findByText(/still needed/i)).toBeInTheDocument()
    expect(screen.getByText(/auth: Field required/)).toBeInTheDocument()
  })

  it('saves a complete draft through the definition rails', async () => {
    const mock = stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: { reply: 'Done.', definition: DRAFTED },
      },
      'POST /api/carrier-builder/check': { body: { valid: true, errors: [] } },
      'POST /api/carriers/acme/definitions/drafts': {
        body: { carrier: 'acme', version: 1, status: 'draft' },
        status: 201,
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')
    await screen.findByText(/complete and ready to save/i)

    await userEvent.type(screen.getByLabelText('Author'), 'jake')
    await userEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    expect(await screen.findByText(/draft v1 of acme saved/i)).toBeInTheDocument()
    expect(sentBody(mock, 'POST /api/carriers/acme/definitions/drafts')).toEqual({
      definition: DRAFTED,
      author: 'jake',
    })
  })

  it('disables save until the definition is complete and an author given', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: {
          reply: 'Started.',
          definition: { carrier: 'acme', name: 'Acme' },
        },
      },
      'POST /api/carrier-builder/check': {
        body: { valid: false, errors: ['auth: Field required'] },
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')
    await screen.findByText(/still needed/i)

    await userEvent.type(screen.getByLabelText('Author'), 'jake')
    // Author present, but the definition is incomplete: save stays disabled.
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeDisabled()
  })

  it('sends the pasted documentation packet with each turn', async () => {
    const mock = stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: { reply: 'Read the docs.', definition: {} },
      },
      'POST /api/carrier-builder/check': { body: { valid: false, errors: [] } },
    })
    renderPage()

    await userEvent.type(
      screen.getByLabelText('Documentation'),
      'Acme API: POST /book',
    )
    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')
    await screen.findByText(/read the docs/i)

    const body = sentBody(mock, 'POST /api/carrier-builder/messages') as {
      packet: string
    }
    expect(body.packet).toBe('Acme API: POST /book')
  })

  it('attaches a text file into the packet', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
    })
    renderPage()

    const file = new File(['Attached spec: GET /labels'], 'spec.txt', {
      type: 'text/plain',
    })
    await userEvent.upload(screen.getByLabelText(/attach a document/i), file)

    await waitFor(() =>
      expect(screen.getByLabelText('Documentation')).toHaveValue(
        'Attached spec: GET /labels',
      ),
    )
  })

  it('stores a credential to carrier config, never the packet', async () => {
    const mock = stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'PATCH /api/carriers/acme/config': {
        body: { carrier: 'acme', status: 'saved', missing: [] },
      },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier code'), 'acme')
    await userEvent.type(screen.getByLabelText('Name'), 'api_key')
    await userEvent.type(screen.getByLabelText('Value'), 'sk-secret')
    await userEvent.click(
      screen.getByRole('button', { name: /store credential/i }),
    )

    expect(await screen.findByText(/stored: api_key/i)).toBeInTheDocument()
    expect(sentBody(mock, 'PATCH /api/carriers/acme/config')).toEqual({
      api_key: 'sk-secret',
    })
    // The secret never entered the packet.
    expect(screen.getByLabelText('Documentation')).toHaveValue('')
  })

  it('shows the still-needed keys after storing a credential, and clears them when the chat moves to a new carrier', async () => {
    // The PATCH response reports what the live definition still needs; a hint from
    // one carrier must not survive into a session drafting another.
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'PATCH /api/carriers/acme/config': {
        body: {
          carrier: 'acme',
          status: 'saved',
          missing: ['config.trackingToken'],
        },
      },
      'POST /api/carrier-builder/messages': {
        body: {
          reply: 'Started globex.',
          definition: { carrier: 'globex', name: 'Globex' },
        },
      },
      'POST /api/carrier-builder/check': { body: { valid: false, errors: [] } },
      'GET /api/carrier-builder/blockers?carrier=globex': { body: [] },
    })
    renderPage()

    await userEvent.type(screen.getByLabelText('Carrier code'), 'acme')
    await userEvent.type(screen.getByLabelText('Name'), 'api_key')
    await userEvent.type(screen.getByLabelText('Value'), 'sk-secret')
    await userEvent.click(
      screen.getByRole('button', { name: /store credential/i }),
    )
    expect(
      await screen.findByText(/still needs: config.trackingToken/i),
    ).toBeInTheDocument()

    const input = screen.getByLabelText(/message the carrier builder/i)
    await userEvent.type(input, 'onboard globex instead{Enter}')
    await screen.findByText(/started globex/i)

    expect(
      screen.queryByText(/still needs: config.trackingToken/i),
    ).not.toBeInTheDocument()
  })

  it('shows the engineering handoffs the turn raised or consumed', async () => {
    // The operator sees what's parked without seeing definition guts: open reads
    // "waiting on engineering", resolved shows the engineer's answer.
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: { reply: 'Parked the signing.', definition: DRAFTED },
      },
      'POST /api/carrier-builder/check': { body: { valid: true, errors: [] } },
      'GET /api/carrier-builder/blockers?carrier=acme': {
        body: [
          {
            id: 1,
            carrier: 'acme',
            kind: 'needs_plugin',
            title: 'HMAC signing',
            detail: 'No plugin.',
            plugin_name: 'acme_hmac',
            status: 'open',
            resolution: null,
            created_at: '2026-07-19T10:00:00Z',
            resolved_at: null,
          },
          {
            id: 2,
            carrier: 'acme',
            kind: 'needs_decision',
            title: 'Which endpoint?',
            detail: 'Two listed.',
            plugin_name: null,
            status: 'resolved',
            resolution: 'Use live.',
            created_at: '2026-07-19T09:00:00Z',
            resolved_at: '2026-07-19T11:00:00Z',
          },
        ],
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')
    await screen.findByText(/parked the signing/i)

    expect(await screen.findByText(/engineering handoffs/i)).toBeInTheDocument()
    expect(screen.getByText('waiting on engineering')).toBeInTheDocument()
    expect(screen.getByText('HMAC signing')).toBeInTheDocument()
    expect(screen.getByText('answered')).toBeInTheDocument()
    expect(screen.getByText(/use live/i)).toBeInTheDocument()
  })

  it('disables save while a turn is in flight', async () => {
    // Saving mid-turn would persist the pre-turn definition while confirming
    // success - the copy on screen is about to be superseded.
    let resolveSecondTurn: (value: Response) => void = () => {}
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    let turn = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const key = `${init?.method ?? 'GET'} ${String(input)}`
        if (key === 'GET /api/carrier-builder/status')
          return json({ configured: true })
        if (key === 'POST /api/carrier-builder/messages') {
          turn += 1
          if (turn === 1) return json({ reply: 'Done.', definition: DRAFTED })
          return new Promise<Response>((resolve) => {
            resolveSecondTurn = resolve
          })
        }
        if (key === 'POST /api/carrier-builder/check')
          return json({ valid: true, errors: [] })
        throw new Error(`unmocked fetch: ${key}`)
      }),
    )
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')
    await screen.findByText(/complete and ready to save/i)
    await userEvent.type(screen.getByLabelText('Author'), 'jake')
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeEnabled()

    // A second turn is in flight: save must disable until it resolves.
    await userEvent.type(input, 'add a tracking operation{Enter}')
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeDisabled()

    resolveSecondTurn(json({ reply: 'Added.', definition: DRAFTED }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /save as draft/i })).toBeEnabled(),
    )
  })

  it('shows a not-configured notice and no input when unconfigured', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: false } },
    })
    renderPage()

    expect(await screen.findByText(/isn.t configured/i)).toBeInTheDocument()
    expect(
      screen.queryByLabelText(/message the carrier builder/i),
    ).not.toBeInTheDocument()
  })

  it('surfaces a builder request failure as an error', async () => {
    stubFetch({
      'GET /api/carrier-builder/status': { body: { configured: true } },
      'POST /api/carrier-builder/messages': {
        body: { detail: 'the carrier builder is unavailable' },
        status: 502,
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the carrier builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'onboard acme{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i)
  })
})
