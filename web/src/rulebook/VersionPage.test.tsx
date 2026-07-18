import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { service, stubFetch, versionDetail } from '@/test/rulebook'

import { VersionPage } from './VersionPage'

const VERSION_1 = versionDetail({
  version: 1,
  status: 'published',
  author: 'seed',
  services: [service()],
})

const VERSION_2 = versionDetail({
  version: 2,
  status: 'draft',
  author: 'jake',
  services: [
    service({ cost: '5.00' }),
    service({ code: 'DROPOUT-XL', name: 'Drop Out Extra Large', tie_break_order: 2 }),
  ],
})

const DRY_RUN = {
  rulebook_version: 2,
  total: 2,
  changed: 1,
  results: [
    {
      order_number: '95000254580',
      current_service: 'DROPOUT-STD',
      draft_service: 'DROPOUT-STD',
      changed: false,
    },
    {
      order_number: '95000254581',
      current_service: null,
      draft_service: 'DROPOUT-XL',
      changed: true,
    },
  ],
}

function renderVersion(version: number) {
  return render(
    <MemoryRouter initialEntries={[`/rulebook/versions/${version}`]}>
      <Routes>
        <Route path="/rulebook/versions/:version" element={<VersionPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('VersionPage', () => {
  it('shows the version description when present', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': {
        body: versionDetail({ version: 2, description: 'Add US shipping for Q4' }),
      },
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
    })
    renderVersion(2)

    expect(
      await screen.findByText('Add US shipping for Q4'),
    ).toBeInTheDocument()
  })

  it('shows metadata, services and the diff against the previous version', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': { body: VERSION_2 },
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
    })
    renderVersion(2)

    expect(
      await screen.findByRole('heading', { name: /version 2/i }),
    ).toBeInTheDocument()
    expect(screen.getByText('draft')).toBeInTheDocument()
    expect(screen.getByText(/jake/)).toBeInTheDocument()

    const diff = await screen.findByRole('region', {
      name: /changes from version 1/i,
    })
    expect(within(diff).getByText('added')).toBeInTheDocument()
    expect(within(diff).getByText('DROPOUT-XL')).toBeInTheDocument()
    expect(within(diff).getByText(/cost/)).toBeInTheDocument()
    expect(within(diff).getByText(/4\.50/)).toBeInTheDocument()
    expect(within(diff).getByText(/5\.00/)).toBeInTheDocument()
  })

  it('marks the live version and calls version 1 the initial version', async () => {
    stubFetch({
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
    })
    renderVersion(1)

    expect(await screen.findByText('live')).toBeInTheDocument()
    expect(screen.getByText(/initial version/i)).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /^publish/i }),
    ).not.toBeInTheDocument()
  })

  it('warns in the publish confirmation when no dry run has been executed', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': { body: VERSION_2 },
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
    })
    renderVersion(2)

    await userEvent.click(
      await screen.findByRole('button', { name: /^publish/i }),
    )

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/no dry run has been executed/i)
  })

  it('publishes after confirming with the dry-run summary shown', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': { body: VERSION_2 },
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
      'POST /api/rulebook/versions/2/dry-run': { body: DRY_RUN },
      'POST /api/rulebook/versions/2/publish': {
        body: { version: 2, status: 'published', author: 'jake' },
      },
    })
    renderVersion(2)

    await userEvent.click(
      await screen.findByRole('button', { name: /run dry run/i }),
    )
    await screen.findByText(/1 of 2 orders would change service/i)
    await userEvent.click(screen.getByRole('button', { name: /^publish/i }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/1 of 2 orders would change service/i)

    await userEvent.click(
      within(dialog).getByRole('button', { name: /publish version 2/i }),
    )

    expect(await screen.findByText('published')).toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('shows the API explanation when publishing is refused', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': { body: VERSION_2 },
      'GET /api/rulebook/versions/1': { body: VERSION_1 },
      'GET /api/rulebook/active': { body: { version: 1, services: [service()] } },
      'POST /api/rulebook/versions/2/publish': {
        status: 409,
        body: { detail: 'version 2 would not become live: version 3 is already published' },
      },
    })
    renderVersion(2)

    await userEvent.click(
      await screen.findByRole('button', { name: /^publish/i }),
    )
    const dialog = await screen.findByRole('alertdialog')
    await userEvent.click(
      within(dialog).getByRole('button', { name: /publish version 2/i }),
    )

    expect(
      await screen.findByText(/would not become live/i),
    ).toBeInTheDocument()
  })
})
