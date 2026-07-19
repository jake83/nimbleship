import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { service, stubFetch } from '@/test/rulebook'

import { VersionsPage } from './VersionsPage'

const VERSIONS = [
  {
    version: 1,
    status: 'published',
    author: 'seed',
    description: null,
    created_at: '2026-07-10T09:00:00Z',
  },
  {
    version: 2,
    status: 'published',
    author: 'jake',
    description: 'Add US shipping for the Q4 launch',
    created_at: '2026-07-11T09:00:00Z',
  },
  {
    version: 3,
    status: 'draft',
    author: 'jake',
    description: null,
    created_at: '2026-07-12T09:00:00Z',
  },
]

function renderPage() {
  return render(
    <MemoryRouter>
      <VersionsPage />
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('VersionsPage', () => {
  it('lists every version, newest first, marking the live one', async () => {
    stubFetch({
      'GET /api/rulebook/versions': { body: VERSIONS },
      'GET /api/rulebook/active': { body: { version: 2, services: [service()] } },
    })
    renderPage()

    const rows = await screen.findAllByRole('row')
    // Header row plus one per version, newest first.
    expect(rows).toHaveLength(4)
    expect(rows[1]).toHaveTextContent('3')
    expect(rows[1]).toHaveTextContent('draft')
    expect(rows[2]).toHaveTextContent('live')
    expect(rows[3]).toHaveTextContent('seed')

    const detailLink = within(rows[1]!).getByRole('link', { name: /version 3/i })
    expect(detailLink).toHaveAttribute('href', '/rulebook/versions/3')
  })

  it('shows each version rationale note in the history list', async () => {
    stubFetch({
      'GET /api/rulebook/versions': { body: VERSIONS },
      'GET /api/rulebook/active': { body: { version: 2, services: [service()] } },
    })
    renderPage()

    // The note an operator reads when scanning history, not just on the detail page.
    const rows = await screen.findAllByRole('row')
    expect(rows[2]).toHaveTextContent('Add US shipping for the Q4 launch')
  })

  it('offers a new draft started from the live version', async () => {
    stubFetch({
      'GET /api/rulebook/versions': { body: VERSIONS },
      'GET /api/rulebook/active': { body: { version: 2, services: [service()] } },
    })
    renderPage()

    // Styled as a button (Base UI exposes role=button), navigates as a link.
    const newDraft = await screen.findByRole('button', { name: /new draft/i })
    expect(newDraft).toHaveAttribute('href', '/rulebook/drafts/new?from=2')
  })

  it('shows the failure when the versions cannot be loaded', async () => {
    stubFetch({
      'GET /api/rulebook/versions': { status: 500, body: { detail: 'boom' } },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
    })
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent(/boom/)
  })
})
