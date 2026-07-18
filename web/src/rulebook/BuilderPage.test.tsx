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
