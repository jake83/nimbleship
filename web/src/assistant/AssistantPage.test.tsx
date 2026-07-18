import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { stubFetch } from '@/test/rulebook'

import { AssistantPage } from './AssistantPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderPage(state?: { initial: string }) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/assistant', state }]}>
      <Routes>
        <Route path="/assistant" element={<AssistantPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AssistantPage', () => {
  it('answers a question and shows both turns', async () => {
    stubFetch({
      'GET /api/assistant/status': { body: { configured: true } },
      'POST /api/assistant/messages': {
        body: { reply: 'It shipped with dropout; the others failed the weight check.' },
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the assistant/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'why did it ship?{Enter}')

    expect(await screen.findByText(/it shipped with dropout/i)).toBeInTheDocument()
    expect(screen.getByText('why did it ship?')).toBeInTheDocument()
  })

  it('sends the launcher question on arrival', async () => {
    stubFetch({
      'GET /api/assistant/status': { body: { configured: true } },
      'POST /api/assistant/messages': { body: { reply: 'Answered from the launcher.' } },
    })
    renderPage({ initial: 'why did 123 fail to print?' })

    expect(await screen.findByText(/answered from the launcher/i)).toBeInTheDocument()
    expect(screen.getByText('why did 123 fail to print?')).toBeInTheDocument()
  })

  it('shows a not-configured notice and no input when unconfigured', async () => {
    stubFetch({ 'GET /api/assistant/status': { body: { configured: false } } })
    renderPage()

    expect(await screen.findByText(/isn.t configured/i)).toBeInTheDocument()
    expect(
      screen.queryByLabelText(/message the assistant/i),
    ).not.toBeInTheDocument()
  })

  it('surfaces a request failure as an error', async () => {
    stubFetch({
      'GET /api/assistant/status': { body: { configured: true } },
      'POST /api/assistant/messages': {
        body: { detail: 'the assistant is unavailable' },
        status: 502,
      },
    })
    renderPage()

    const input = await screen.findByLabelText(/message the assistant/i)
    await waitFor(() => expect(input).toBeEnabled())
    await userEvent.type(input, 'why?{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent(/unavailable/i)
  })
})
