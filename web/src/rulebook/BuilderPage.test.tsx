import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { sentBody, service, stubFetch } from '@/test/rulebook'

import { BuilderPage } from './BuilderPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const ACTIVE = { version: 1, services: [service({ code: 'DROPOUT-STD' })] }

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/rulebook/builder']}>
      <Routes>
        <Route path="/rulebook/builder" element={<BuilderPage />} />
        <Route
          path="/rulebook/versions/:version"
          element={<div>version page</div>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('BuilderPage', () => {
  it('surfaces a restriction the AI just added to the working copy', async () => {
    // A proposition/group/area restriction the AI adds must be visible in the review
    // panel before the operator saves, not only summarised in the chat bubble.
    const restricted = service({
      code: 'DROPOUT-STD',
      propositions: ['SIGNATURE_REQUIRED'],
    })
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
      'POST /api/rulebook/builder/messages': {
        body: { reply: 'Restricted it.', services: [restricted] },
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the rules builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'restrict to signature required{Enter}')

    expect(await screen.findByText(/restricted it/i)).toBeInTheDocument()
    expect(screen.getByText(/propositions: SIGNATURE_REQUIRED/i)).toBeInTheDocument()
  })

  it('reads an empty areas-served list as blocked everywhere, not blank', async () => {
    // areas_served null = anywhere, [] = nowhere (the most severe value). [] must
    // read legibly, not as a blank "areas served: " that looks like a glitch.
    const blocked = service({ code: 'DROPOUT-STD', areas_served: [] })
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: { version: 1, services: [blocked] } },
    })
    renderPage()

    expect(
      await screen.findByText(/areas served: none - blocked everywhere/i),
    ).toBeInTheDocument()
  })

  it('previews the working copy impact over recent orders', async () => {
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
      'POST /api/rulebook/builder/dry-run': {
        body: {
          total: 3,
          changed: 1,
          results: [
            {
              order_number: 'A1',
              current_service: 'DROPOUT-STD',
              draft_service: 'CHEAP',
              changed: true,
            },
            {
              order_number: 'A2',
              current_service: 'DROPOUT-STD',
              draft_service: 'DROPOUT-STD',
              changed: false,
            },
          ],
        },
      },
    })
    renderPage()

    await screen.findByText('DROPOUT-STD')
    await userEvent.click(screen.getByRole('button', { name: /preview impact/i }))

    expect(
      await screen.findByText(/1 of 3 recent orders would change service/i),
    ).toBeInTheDocument()
    // Only the changed order is listed, with its from -> to.
    expect(screen.getByText(/A1/)).toBeInTheDocument()
    expect(screen.queryByText(/A2/)).not.toBeInTheDocument()
  })

  it('seeds the working copy from the live rulebook and applies an edit', async () => {
    const added = service({ code: 'FR-NEXT-DAY', carrier: 'zip' })
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
      'POST /api/rulebook/builder/messages': {
        body: { reply: 'Added FR-NEXT-DAY.', services: [...ACTIVE.services, added] },
      },
    })
    renderPage()

    // The seeded service shows before the first turn.
    expect(await screen.findByText('DROPOUT-STD')).toBeInTheDocument()

    const input = await screen.findByLabelText(/message the rules builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'add a next-day service for France{Enter}')

    expect(await screen.findByText(/added fr-next-day/i)).toBeInTheDocument()
    // The working copy panel now carries the model's edit.
    expect(await screen.findByText('FR-NEXT-DAY')).toBeInTheDocument()
  })

  it('saves the working copy as a draft and navigates to the version', async () => {
    const mock = stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
      'POST /api/rulebook/drafts': {
        body: { version: 7, status: 'draft', author: 'jake', description: null },
        status: 201,
      },
    })
    renderPage()

    await screen.findByText('DROPOUT-STD')
    await userEvent.type(screen.getByLabelText('Author'), 'jake')
    await userEvent.type(
      screen.getByLabelText(/description/i),
      'Add French next-day',
    )
    await userEvent.click(screen.getByRole('button', { name: /save as draft/i }))

    expect(await screen.findByText('version page')).toBeInTheDocument()
    expect(sentBody(mock, 'POST /api/rulebook/drafts')).toEqual({
      services: ACTIVE.services,
      author: 'jake',
      description: 'Add French next-day',
    })
  })

  it('disables save until an author is given', async () => {
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
    })
    renderPage()

    await screen.findByText('DROPOUT-STD')
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeDisabled()

    await userEvent.type(screen.getByLabelText('Author'), 'jake')
    expect(screen.getByRole('button', { name: /save as draft/i })).toBeEnabled()
  })

  it('shows a not-configured notice and no input when unconfigured', async () => {
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: false } },
      'GET /api/rulebook/active': { body: ACTIVE },
    })
    renderPage()

    expect(await screen.findByText(/isn.t configured/i)).toBeInTheDocument()
    expect(
      screen.queryByLabelText(/message the rules builder/i),
    ).not.toBeInTheDocument()
  })

  it('keeps the input disabled until the live rulebook seed has loaded', async () => {
    // The status check and the seed fetch race. The client sends whatever `services`
    // it holds each turn, and an empty [] is a legal working copy server-side, so
    // sending before the seed lands would silently build from scratch instead of the
    // live rulebook. The input must stay disabled until the seed resolves.
    let resolveActive: (value: Response) => void = () => {}
    const activePending = new Promise<Response>((resolve) => {
      resolveActive = resolve
    })
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const key = `${init?.method ?? 'GET'} ${String(input)}`
        if (key === 'GET /api/rulebook/builder/status')
          return json({ configured: true })
        if (key === 'GET /api/rulebook/active') return activePending
        throw new Error(`unmocked fetch: ${key}`)
      }),
    )
    renderPage()

    // Status has resolved (configured), but the seed has not - input stays disabled.
    const input = await screen.findByLabelText(/message the rules builder/i)
    await waitFor(() => expect(input).toBeDisabled())
    expect(screen.getByText(/loading the current rulebook/i)).toBeInTheDocument()

    // Seed lands: input enables and the live service appears in the working copy.
    resolveActive(json(ACTIVE))
    await waitFor(() => expect(input).toBeEnabled())
    expect(screen.getByText('DROPOUT-STD')).toBeInTheDocument()
  })

  it('surfaces a failure to load the live rulebook seed', async () => {
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: { detail: 'boom' }, status: 500 },
    })
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /couldn.t load the current rulebook/i,
    )
    // Without a seed the input is disabled rather than silently starting empty.
    expect(
      await screen.findByLabelText(/message the rules builder/i),
    ).toBeDisabled()
  })

  it('surfaces a builder request failure as an error', async () => {
    stubFetch({
      'GET /api/rulebook/builder/status': { body: { configured: true } },
      'GET /api/rulebook/active': { body: ACTIVE },
      'POST /api/rulebook/builder/messages': {
        body: { detail: 'the rules builder is unavailable' },
        status: 502,
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the rules builder/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'add a service{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i)
  })
})
