import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

import { service, stubFetch } from '@/test/rulebook'

import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('renders the NimbleShip heading', () => {
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    )

    expect(
      screen.getByRole('heading', { name: /nimbleship/i }),
    ).toBeInTheDocument()
  })

  it('navigates to the Rulebook section', async () => {
    stubFetch({
      'GET /api/rulebook/versions': {
        body: [
          {
            version: 1,
            status: 'published',
            author: 'seed',
            created_at: '2026-07-10T09:00:00Z',
          },
        ],
      },
      'GET /api/rulebook/active': {
        body: { version: 1, services: [service()] },
      },
    })
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    )

    const nav = screen.getByRole('navigation')
    await userEvent.click(within(nav).getByRole('link', { name: /^rulebook$/i }))

    expect(
      await screen.findByRole('heading', { name: /rulebook/i }),
    ).toBeInTheDocument()
    expect(
      await screen.findByRole('link', { name: /version 1/i }),
    ).toBeInTheDocument()
  })
})
