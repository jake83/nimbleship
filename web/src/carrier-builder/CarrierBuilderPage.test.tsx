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

const DRAFTED = {
  carrier: 'acme',
  name: 'Acme',
  auth: { scheme: 'none' },
  operations: { book: { steps: [] } },
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
