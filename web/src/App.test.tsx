import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'

import { service, stubFetch } from '@/test/rulebook'

import App from './App'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('App', () => {
  it('renders the NimbleShip heading', () => {
    stubFetch({
      'GET /api/assistant/status': { body: { configured: false } },
    })
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
      'GET /api/assistant/status': { body: { configured: false } },
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

  it('launches an assistant question from the homepage', async () => {
    stubFetch({
      'GET /api/assistant/status': { body: { configured: true } },
      'POST /api/assistant/messages': { body: { reply: 'It shipped with dropout.' } },
    })
    render(
      <MemoryRouter>
        <App />
      </MemoryRouter>,
    )

    const input = await screen.findByLabelText(/message the assistant/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'why did 123 ship?{Enter}')

    // Navigated to the assistant page and the question was answered end to end.
    expect(
      await screen.findByRole('heading', { name: /assistant/i }),
    ).toBeInTheDocument()
    expect(await screen.findByText(/it shipped with dropout/i)).toBeInTheDocument()
    expect(screen.getByText('why did 123 ship?')).toBeInTheDocument()
  })
})
