import { sentBody, service, stubFetch } from '@/test/rulebook'

import {
  dryRunWorkingCopy,
  fetchBuilderStatus,
  sendBuilderMessages,
} from './builder-api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('rules builder api', () => {
  it('posts the conversation and working copy, returns the edited copy', async () => {
    const edited = service({ code: 'NEW-SVC' })
    const mock = stubFetch({
      'POST /api/rulebook/builder/messages': {
        body: { reply: 'Added it.', services: [edited] },
      },
    })
    const messages = [{ role: 'user' as const, content: 'add a service' }]
    const services = [service()]

    const result = await sendBuilderMessages(messages, services)

    expect(result.reply).toBe('Added it.')
    expect(result.services).toEqual([edited])
    expect(sentBody(mock, 'POST /api/rulebook/builder/messages')).toEqual({
      messages,
      services,
    })
  })

  it('posts the working copy for a dry run and returns the impact', async () => {
    const mock = stubFetch({
      'POST /api/rulebook/builder/dry-run': {
        body: { total: 2, changed: 1, results: [] },
      },
    })
    const services = [service()]

    const outcome = await dryRunWorkingCopy(services)

    expect(outcome).toEqual({ total: 2, changed: 1, results: [] })
    expect(sentBody(mock, 'POST /api/rulebook/builder/dry-run')).toEqual({
      services,
    })
  })

  it('reads the configured status', async () => {
    stubFetch({ 'GET /api/rulebook/builder/status': { body: { configured: true } } })

    expect(await fetchBuilderStatus()).toEqual({ configured: true })
  })

  it('throws BuilderError carrying the server detail', async () => {
    stubFetch({
      'POST /api/rulebook/builder/messages': {
        body: { detail: 'the rules builder is not configured' },
        status: 503,
      },
    })

    await expect(
      sendBuilderMessages([{ role: 'user', content: 'x' }], []),
    ).rejects.toMatchObject({
      status: 503,
      message: 'the rules builder is not configured',
    })
  })

  it('surfaces the first message of a 422 validation error', async () => {
    stubFetch({
      'POST /api/rulebook/builder/messages': {
        body: {
          detail: [
            { loc: ['body', 'messages'], msg: 'List should have at most 50 items' },
          ],
        },
        status: 422,
      },
    })

    await expect(
      sendBuilderMessages([{ role: 'user', content: 'x' }], []),
    ).rejects.toMatchObject({
      status: 422,
      message: 'List should have at most 50 items',
    })
  })
})
