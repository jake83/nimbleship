import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { sentBody, stubFetch } from '@/test/rulebook'

import { ShippingAreaFormPage } from './ShippingAreaFormPage'
import { ShippingAreasPage } from './ShippingAreasPage'

afterEach(() => {
  vi.unstubAllGlobals()
})

const AREAS = [
  {
    code: 'highlands',
    name: 'Scottish Highlands',
    country: 'GB',
    prefixes: ['AB', 'IV', 'KW', 'PA', 'PH', 'HS', 'ZE'],
  },
  {
    code: 'london',
    name: 'London',
    country: 'GB',
    prefixes: ['E', 'EC', 'N'],
  },
]

function renderList() {
  return render(
    <MemoryRouter initialEntries={['/shipping-areas']}>
      <Routes>
        <Route path="/shipping-areas" element={<ShippingAreasPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function renderForm(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/shipping-areas" element={<ShippingAreasPage />} />
        <Route path="/shipping-areas/new" element={<ShippingAreaFormPage />} />
        <Route path="/shipping-areas/:code" element={<ShippingAreaFormPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('ShippingAreasPage', () => {
  it('lists areas with a prefix preview that expands to the full set', async () => {
    stubFetch({ 'GET /api/shipping-areas': { body: AREAS } })
    renderList()

    expect(await screen.findByText('Scottish Highlands')).toBeInTheDocument()
    // Preview keeps the row scannable: the first few prefixes plus a count.
    expect(screen.getByText(/AB, IV, KW, PA, PH \+2 more/)).toBeInTheDocument()
    expect(screen.queryByText(/ZE/)).not.toBeInTheDocument()

    await userEvent.click(
      screen.getByRole('button', { name: /show all 7 prefixes/i }),
    )
    expect(screen.getByText(/AB, IV, KW, PA, PH, HS, ZE/)).toBeInTheDocument()
  })

  it('filters by name, code, or postcode prefix', async () => {
    stubFetch({ 'GET /api/shipping-areas': { body: AREAS } })
    renderList()
    await screen.findByText('Scottish Highlands')

    const search = screen.getByLabelText(/search/i)
    await userEvent.type(search, 'EC')
    expect(screen.queryByText('Scottish Highlands')).not.toBeInTheDocument()
    expect(screen.getByText('London')).toBeInTheDocument()

    await userEvent.clear(search)
    await userEvent.type(search, 'highl')
    expect(screen.getByText('Scottish Highlands')).toBeInTheDocument()
    expect(screen.queryByText('London')).not.toBeInTheDocument()
  })

  it('creates an area from the form, splitting pasted prefixes', async () => {
    const mock = stubFetch({
      'GET /api/shipping-areas': { body: [] },
      'POST /api/shipping-areas': {
        status: 201,
        body: {
          code: 'highlands',
          name: 'Scottish Highlands',
          country: 'GB',
          prefixes: ['AB', 'IV'],
        },
      },
    })
    renderForm('/shipping-areas/new')

    await userEvent.type(screen.getByLabelText('Code'), 'highlands')
    await userEvent.type(screen.getByLabelText('Name'), 'Scottish Highlands')
    await userEvent.type(screen.getByLabelText('Country'), 'gb')
    await userEvent.type(
      screen.getByLabelText(/postcode prefixes/i),
      'AB, IV\niv ',
    )
    await userEvent.click(screen.getByRole('button', { name: /save/i }))

    // Back on the list after a successful save.
    expect(
      await screen.findByRole('heading', { name: /shipping areas/i }),
    ).toBeInTheDocument()
    expect(sentBody(mock, 'POST /api/shipping-areas')).toEqual({
      code: 'highlands',
      name: 'Scottish Highlands',
      country: 'gb',
      prefixes: ['AB', 'IV', 'iv'],
    })
  })

  it('edits an existing area with its code fixed', async () => {
    const mock = stubFetch({
      'GET /api/shipping-areas/london': { body: AREAS[1] },
      'PUT /api/shipping-areas/london': {
        body: {
          code: 'london',
          name: 'Greater London',
          country: 'GB',
          prefixes: ['E', 'EC', 'N'],
        },
      },
    })
    renderForm('/shipping-areas/london')

    const name = await screen.findByLabelText('Name')
    await waitFor(() => expect(name).toHaveValue('London'))
    // The code is the area's identity; it is shown but not editable.
    expect(screen.getByLabelText('Code')).toBeDisabled()

    await userEvent.clear(name)
    await userEvent.type(name, 'Greater London')
    await userEvent.click(screen.getByRole('button', { name: /save/i }))

    await waitFor(() =>
      expect(sentBody(mock, 'PUT /api/shipping-areas/london')).toEqual({
        name: 'Greater London',
        country: 'GB',
        prefixes: ['E', 'EC', 'N'],
      }),
    )
  })

  it('surfaces a server refusal on the form', async () => {
    stubFetch({
      'GET /api/shipping-areas': { body: [] },
      'POST /api/shipping-areas': {
        status: 409,
        body: { detail: 'a shipping area already exists with this code' },
      },
    })
    renderForm('/shipping-areas/new')

    await userEvent.type(screen.getByLabelText('Code'), 'highlands')
    await userEvent.type(screen.getByLabelText('Name'), 'Highlands')
    await userEvent.type(screen.getByLabelText('Country'), 'GB')
    await userEvent.type(screen.getByLabelText(/postcode prefixes/i), 'AB')
    await userEvent.click(screen.getByRole('button', { name: /save/i }))

    expect(
      await screen.findByText(/already exists with this code/i),
    ).toBeInTheDocument()
  })
})
