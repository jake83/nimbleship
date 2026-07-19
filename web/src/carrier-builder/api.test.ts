import { sentBody, stubFetch } from '@/test/rulebook'

import {
  checkDefinition,
  createDefinitionDraft,
  fetchBuilderStatus,
  sendBuilderMessages,
} from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('carrier builder api', () => {
  it('posts the conversation and working definition, returns the edited copy', async () => {
    const edited = { carrier: 'acme', name: 'Acme' }
    const mock = stubFetch({
      'POST /api/carrier-builder/messages': {
        body: { reply: 'Set the identity.', definition: edited },
      },
    })
    const messages = [{ role: 'user' as const, content: 'onboard acme' }]

    const result = await sendBuilderMessages(messages, {})

    expect(result.reply).toBe('Set the identity.')
    expect(result.definition).toEqual(edited)
    expect(sentBody(mock, 'POST /api/carrier-builder/messages')).toEqual({
      messages,
      definition: {},
    })
  })

  it('checks the working definition', async () => {
    stubFetch({
      'POST /api/carrier-builder/check': {
        body: { valid: false, errors: ['operations: Field required'] },
      },
    })

    const outcome = await checkDefinition({ carrier: 'acme' })

    expect(outcome.valid).toBe(false)
    expect(outcome.errors[0]).toContain('operations')
  })

  it('creates a definition draft on the carrier rails', async () => {
    const mock = stubFetch({
      'POST /api/carriers/acme/definitions/drafts': {
        body: { carrier: 'acme', version: 1, status: 'draft' },
        status: 201,
      },
    })
    const definition = { carrier: 'acme', name: 'Acme' }

    const created = await createDefinitionDraft('acme', definition, 'jake')

    expect(created.version).toBe(1)
    expect(sentBody(mock, 'POST /api/carriers/acme/definitions/drafts')).toEqual({
      definition,
      author: 'jake',
    })
  })

  it('reads the configured status', async () => {
    stubFetch({ 'GET /api/carrier-builder/status': { body: { configured: true } } })

    expect(await fetchBuilderStatus()).toEqual({ configured: true })
  })

  it('throws BuilderError carrying the server detail', async () => {
    stubFetch({
      'POST /api/carrier-builder/messages': {
        body: { detail: 'the carrier builder is not configured' },
        status: 503,
      },
    })

    await expect(
      sendBuilderMessages([{ role: 'user', content: 'x' }], {}),
    ).rejects.toMatchObject({
      status: 503,
      message: 'the carrier builder is not configured',
    })
  })
})
