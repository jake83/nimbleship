import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { sentBody, stubFetch } from '@/test/rulebook'

import { CarrierConfigPage } from './CarrierConfigPage'
import { CarriersPage } from './CarriersPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

function renderCarriers() {
  return render(
    <MemoryRouter initialEntries={['/carriers']}>
      <Routes>
        <Route path="/carriers" element={<CarriersPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderConfig(carrier: string) {
  return render(
    <MemoryRouter initialEntries={[`/carriers/${carrier}/config`]}>
      <Routes>
        <Route path="/carriers/:carrier/config" element={<CarrierConfigPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('CarriersPage', () => {
  it('lists carriers with their active version and a config link', async () => {
    stubFetch({
      'GET /api/carriers': {
        body: [
          { carrier: 'acme', active_version: 3 },
          { carrier: 'newco', active_version: null },
        ],
      },
    })
    renderCarriers()

    expect(await screen.findByText('acme')).toBeInTheDocument()
    expect(screen.getByText('v3 live')).toBeInTheDocument()
    expect(screen.getByText('no published definition')).toBeInTheDocument()
    // The Button render-as-Link pattern exposes role button, not link.
    expect(screen.getAllByRole('button', { name: /config/i })).toHaveLength(2)
  })
})

describe('CarrierConfigPage', () => {
  it('flags a stored-but-null key as still required', async () => {
    stubFetch({
      'GET /api/carriers/acme/config': {
        body: {
          carrier: 'acme',
          config: { api_key: 'K-1', base_url: null },
          missing: ['base_url'],
        },
      },
    })
    renderConfig('acme')

    expect(await screen.findByLabelText('base_url')).toBeInTheDocument()
    // Stored null renders as nothing at booking: the badge must survive presence.
    expect(
      screen.getByText(/required by the active definition/i),
    ).toBeInTheDocument()
  })

  it('routes a nested missing path to its containing key, never a flat input', async () => {
    const mock = stubFetch({
      'GET /api/carriers/acme/config': {
        body: { carrier: 'acme', config: {}, missing: ['depot.code'] },
      },
      'PUT /api/carriers/acme/config': {
        body: { carrier: 'acme', status: 'saved', missing: [] },
      },
    })
    renderConfig('acme')

    expect(await screen.findByText(/nested values/i)).toBeInTheDocument()
    expect(screen.getByText('depot.code')).toBeInTheDocument()
    // No fillable flat input for the dotted path - it would save a junk key.
    expect(screen.queryByLabelText('depot.code')).not.toBeInTheDocument()

    // The containing key is added as JSON and saves structured.
    await userEvent.type(screen.getByLabelText('New key'), 'depot')
    await userEvent.click(screen.getByLabelText('New value'))
    await userEvent.paste('{"code":"MAN1"}')
    await userEvent.click(screen.getByRole('button', { name: /add/i }))
    await userEvent.click(screen.getByRole('button', { name: /^save/i }))
    await waitFor(() =>
      expect(sentBody(mock, 'PUT /api/carriers/acme/config')).toEqual({
        depot: { code: 'MAN1' },
      }),
    )
  })

  it('masks values until revealed', async () => {
    stubFetch({
      'GET /api/carriers/acme/config': {
        body: { carrier: 'acme', config: { api_key: 'K-1' }, missing: [] },
      },
    })
    renderConfig('acme')

    const input = await screen.findByLabelText('api_key')
    expect(input).toHaveAttribute('type', 'password')
    await userEvent.click(screen.getByRole('button', { name: /show api_key/i }))
    expect(input).toHaveAttribute('type', 'text')
  })

  it('shows stored entries and the keys the definition still needs', async () => {
    stubFetch({
      'GET /api/carriers/acme/config': {
        body: {
          carrier: 'acme',
          config: { api_key: 'K-1' },
          missing: ['base_url'],
        },
      },
    })
    renderConfig('acme')

    expect(await screen.findByLabelText('api_key')).toHaveValue('K-1')
    // A referenced-but-unstored key appears ready to fill, flagged as needed.
    expect(screen.getByLabelText('base_url')).toHaveValue('')
    expect(
      screen.getByText(/required by the active definition/i),
    ).toBeInTheDocument()
  })

  it('saves the full edited set, including added and removed keys', async () => {
    const mock = stubFetch({
      'GET /api/carriers/acme/config': {
        body: {
          carrier: 'acme',
          config: { api_key: 'K-1', old_key: 'gone' },
          missing: [],
        },
      },
      'PUT /api/carriers/acme/config': {
        body: { carrier: 'acme', status: 'saved', missing: [] },
      },
    })
    renderConfig('acme')

    const apiKey = await screen.findByLabelText('api_key')
    await userEvent.clear(apiKey)
    await userEvent.type(apiKey, 'K-2')
    await userEvent.click(screen.getByRole('button', { name: /remove old_key/i }))

    await userEvent.type(screen.getByLabelText('New key'), 'depot')
    await userEvent.type(screen.getByLabelText('New value'), 'MAN1')
    await userEvent.click(screen.getByRole('button', { name: /add/i }))

    await userEvent.click(screen.getByRole('button', { name: /^save/i }))

    expect(await screen.findByText(/saved/i)).toBeInTheDocument()
    expect(sentBody(mock, 'PUT /api/carriers/acme/config')).toEqual({
      api_key: 'K-2',
      depot: 'MAN1',
    })
  })

  it('round-trips a structured value as JSON and refuses invalid JSON', async () => {
    const mock = stubFetch({
      'GET /api/carriers/acme/config': {
        body: {
          carrier: 'acme',
          config: { depot: { code: 'MAN1' } },
          missing: [],
        },
      },
      'PUT /api/carriers/acme/config': {
        body: { carrier: 'acme', status: 'saved', missing: [] },
      },
    })
    renderConfig('acme')

    const depot = await screen.findByLabelText('depot')
    expect(depot).toHaveValue('{"code":"MAN1"}')

    await userEvent.clear(depot)
    await userEvent.type(depot, 'not json')
    await userEvent.click(screen.getByRole('button', { name: /^save/i }))
    expect(await screen.findByText(/not valid JSON/i)).toBeInTheDocument()

    await userEvent.clear(depot)
    await userEvent.paste('{"code":"LDS2"}')
    await userEvent.click(screen.getByRole('button', { name: /^save/i }))
    await waitFor(() =>
      expect(sentBody(mock, 'PUT /api/carriers/acme/config')).toEqual({
        depot: { code: 'LDS2' },
      }),
    )
  })
})
