import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { sentBody, service, stubFetch, versionDetail } from '@/test/rulebook'

import { DraftEditorPage } from './DraftEditorPage'

const SEED = versionDetail({
  version: 2,
  status: 'published',
  services: [
    service(),
    service({ code: 'DROPOUT-XL', name: 'Drop Out Extra Large', tie_break_order: 2 }),
  ],
})

function renderEditor() {
  return render(
    <MemoryRouter initialEntries={['/rulebook/drafts/new?from=2']}>
      <Routes>
        <Route path="/rulebook/drafts/new" element={<DraftEditorPage />} />
        <Route
          path="/rulebook/versions/:version"
          element={<div>version page probe</div>}
        />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('DraftEditorPage', () => {
  it('seeds the form from the version named in ?from', async () => {
    stubFetch({ 'GET /api/rulebook/versions/2': { body: SEED } })
    renderEditor()

    const codes = await screen.findAllByLabelText(/^code$/i)
    expect(codes.map((input) => (input as HTMLInputElement).value)).toEqual([
      'DROPOUT-STD',
      'DROPOUT-XL',
    ])
  })

  it('flags a cleared required field and disables save', async () => {
    stubFetch({ 'GET /api/rulebook/versions/2': { body: SEED } })
    renderEditor()

    const [code] = await screen.findAllByLabelText(/^code$/i)
    await userEvent.type(screen.getByLabelText(/author/i), 'jake')
    await userEvent.clear(code!)

    expect(await screen.findByText(/required/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save draft/i })).toBeDisabled()
  })

  it('flags duplicate service codes', async () => {
    stubFetch({ 'GET /api/rulebook/versions/2': { body: SEED } })
    renderEditor()

    const codes = await screen.findAllByLabelText(/^code$/i)
    await userEvent.type(screen.getByLabelText(/author/i), 'jake')
    await userEvent.clear(codes[1]!)
    await userEvent.type(codes[1]!, 'DROPOUT-STD')

    expect(
      await screen.findByText(/duplicate service code/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save draft/i })).toBeDisabled()
  })

  it('flags a weight range whose minimum exceeds its maximum', async () => {
    stubFetch({ 'GET /api/rulebook/versions/2': { body: SEED } })
    renderEditor()

    const [weightMin] = await screen.findAllByLabelText(/weight min/i)
    await userEvent.clear(weightMin!)
    await userEvent.type(weightMin!, '99')

    expect(
      await screen.findByText(/at least the minimum weight/i),
    ).toBeInTheDocument()
  })

  it('saves a valid draft and moves to the new version', async () => {
    const mock = stubFetch({
      'GET /api/rulebook/versions/2': { body: SEED },
      'POST /api/rulebook/drafts': {
        status: 201,
        body: { version: 3, status: 'draft', author: 'jake' },
      },
    })
    renderEditor()

    const [cost] = await screen.findAllByLabelText(/^cost/i)
    await userEvent.clear(cost!)
    await userEvent.type(cost!, '5.00')
    await userEvent.type(screen.getByLabelText(/author/i), 'jake')
    await userEvent.click(screen.getByRole('button', { name: /save draft/i }))

    expect(await screen.findByText('version page probe')).toBeInTheDocument()
    const body = sentBody(mock, 'POST /api/rulebook/drafts') as {
      author: string
      services: Array<Record<string, unknown>>
    }
    expect(body.author).toBe('jake')
    expect(body.services).toHaveLength(2)
    expect(body.services[0]).toMatchObject({
      code: 'DROPOUT-STD',
      cost: '5.00',
      countries: ['GB'],
      tie_break_order: 1,
    })
  })

  it('shows the API explanation when the draft is refused', async () => {
    stubFetch({
      'GET /api/rulebook/versions/2': { body: SEED },
      'POST /api/rulebook/drafts': {
        status: 422,
        body: { detail: 'duplicate tie-break order: 2' },
      },
    })
    renderEditor()

    await screen.findAllByLabelText(/^code$/i)
    await userEvent.type(screen.getByLabelText(/author/i), 'jake')
    await userEvent.click(screen.getByRole('button', { name: /save draft/i }))

    expect(
      await screen.findByText(/duplicate tie-break order: 2/i),
    ).toBeInTheDocument()
  })

  it('can add and remove services', async () => {
    stubFetch({ 'GET /api/rulebook/versions/2': { body: SEED } })
    renderEditor()

    await screen.findAllByLabelText(/^code$/i)
    await userEvent.type(screen.getByLabelText(/author/i), 'jake')
    await userEvent.click(screen.getByRole('button', { name: /add service/i }))
    expect(screen.getAllByLabelText(/^code$/i)).toHaveLength(3)

    const removeButtons = screen.getAllByRole('button', {
      name: /remove service/i,
    })
    await userEvent.click(removeButtons[2]!)
    await userEvent.click(removeButtons[1]!)
    await userEvent.click(removeButtons[0]!)
    expect(
      screen.getByText(/at least one service/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save draft/i })).toBeDisabled()
  })
})
