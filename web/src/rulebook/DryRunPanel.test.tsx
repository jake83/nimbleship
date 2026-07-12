import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { sentBody, stubFetch } from '@/test/rulebook'

import { DryRunPanel } from './DryRunPanel'

const OUTCOME = {
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DryRunPanel', () => {
  it('replays recent orders and highlights the changed rows', async () => {
    const mock = stubFetch({
      'POST /api/rulebook/versions/2/dry-run': { body: OUTCOME },
    })
    render(<DryRunPanel version={2} />)

    await userEvent.click(screen.getByRole('button', { name: /run dry run/i }))

    expect(
      await screen.findByText(/1 of 2 orders would change service/i),
    ).toBeInTheDocument()
    const changedRow = screen.getByRole('row', { name: /95000254581/ })
    const unchangedRow = screen.getByRole('row', { name: /95000254580/ })
    expect(changedRow).toHaveAttribute('data-changed', 'true')
    expect(unchangedRow).toHaveAttribute('data-changed', 'false')
    expect(sentBody(mock, 'POST /api/rulebook/versions/2/dry-run')).toEqual({
      limit: 50,
    })
  })

  it('shows no allocation as such rather than as a service code', async () => {
    stubFetch({
      'POST /api/rulebook/versions/2/dry-run': { body: OUTCOME },
    })
    render(<DryRunPanel version={2} />)

    await userEvent.click(screen.getByRole('button', { name: /run dry run/i }))

    const changedRow = await screen.findByRole('row', { name: /95000254581/ })
    expect(changedRow).toHaveTextContent('no allocation')
  })

  it('sends the requested number of recent orders', async () => {
    const mock = stubFetch({
      'POST /api/rulebook/versions/2/dry-run': { body: OUTCOME },
    })
    render(<DryRunPanel version={2} />)

    const limit = screen.getByLabelText(/recent orders to replay/i)
    await userEvent.clear(limit)
    await userEvent.type(limit, '10')
    await userEvent.click(screen.getByRole('button', { name: /run dry run/i }))

    expect(sentBody(mock, 'POST /api/rulebook/versions/2/dry-run')).toEqual({
      limit: 10,
    })
  })

  it('sends specific order numbers when that mode is chosen', async () => {
    const mock = stubFetch({
      'POST /api/rulebook/versions/2/dry-run': { body: OUTCOME },
    })
    render(<DryRunPanel version={2} />)

    await userEvent.click(
      screen.getByRole('button', { name: /specific order numbers/i }),
    )
    await userEvent.type(
      screen.getByLabelText(/order numbers/i),
      '95000254580{enter}95000254581, 95000254582',
    )
    await userEvent.click(screen.getByRole('button', { name: /run dry run/i }))

    expect(sentBody(mock, 'POST /api/rulebook/versions/2/dry-run')).toEqual({
      order_numbers: ['95000254580', '95000254581', '95000254582'],
    })
  })

  it('shows the API explanation when the dry run is refused', async () => {
    stubFetch({
      'POST /api/rulebook/versions/2/dry-run': {
        status: 404,
        body: { detail: 'no such rulebook version' },
      },
    })
    render(<DryRunPanel version={2} />)

    await userEvent.click(screen.getByRole('button', { name: /run dry run/i }))

    expect(
      await screen.findByText(/no such rulebook version/i),
    ).toBeInTheDocument()
  })
})
