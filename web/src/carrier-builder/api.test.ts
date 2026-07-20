import { sentBody, stubFetch } from '@/test/rulebook'

import {
  checkDefinition,
  createDefinitionDraft,
  fetchBlockers,
  fetchBuilderStatus,
  resolveBlocker,
  saveCredentials,
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

    const result = await sendBuilderMessages(messages, {}, 'Acme API docs.', {
      manifest: 'no end-of-day process',
    })

    expect(result.reply).toBe('Set the identity.')
    expect(result.definition).toEqual(edited)
    expect(sentBody(mock, 'POST /api/carrier-builder/messages')).toEqual({
      messages,
      definition: {},
      packet: 'Acme API docs.',
      not_applicable: { manifest: 'no end-of-day process' },
    })
  })

  it('stores a credential via the carrier config rails, never the packet', async () => {
    const mock = stubFetch({
      'PATCH /api/carriers/acme/config': {
        body: { carrier: 'acme', status: 'saved', missing: [] },
      },
    })

    const saved = await saveCredentials('acme', { api_key: 'sk-secret' })

    expect(saved.status).toBe('saved')
    expect(sentBody(mock, 'PATCH /api/carriers/acme/config')).toEqual({
      api_key: 'sk-secret',
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

  it('fetches a carrier blockers queue and resolves one', async () => {
    const blocker = {
      id: 7,
      carrier: 'acme',
      kind: 'needs_plugin',
      title: 'HMAC signing',
      detail: 'No plugin.',
      plugin_name: 'acme_hmac',
      status: 'open',
      resolution: null,
      created_at: '2026-07-19T10:00:00Z',
      resolved_at: null,
    }
    const mock = stubFetch({
      'GET /api/carrier-builder/blockers?carrier=acme': { body: [blocker] },
      'POST /api/carrier-builder/blockers/7/resolve': {
        body: { ...blocker, status: 'resolved', resolution: 'Shipped in v2.' },
      },
    })

    const queue = await fetchBlockers('acme')
    expect(queue[0]!.title).toBe('HMAC signing')

    const resolved = await resolveBlocker(7, 'Shipped in v2.')
    expect(resolved.status).toBe('resolved')
    expect(sentBody(mock, 'POST /api/carrier-builder/blockers/7/resolve')).toEqual(
      { resolution: 'Shipped in v2.' },
    )
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
      sendBuilderMessages([{ role: 'user', content: 'x' }], {}, '', {}),
    ).rejects.toMatchObject({
      status: 503,
      message: 'the carrier builder is not configured',
    })
  })
})
