import { sentBody, stubFetch } from '@/test/rulebook'

import { fetchAssistantStatus, sendAssistantMessages } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('assistant api', () => {
  it('posts the whole conversation and returns the reply', async () => {
    const mock = stubFetch({
      'POST /api/assistant/messages': { body: { reply: 'It shipped with dropout.' } },
    })
    const messages = [{ role: 'user' as const, content: 'why did it ship?' }]

    const result = await sendAssistantMessages(messages)

    expect(result.reply).toBe('It shipped with dropout.')
    expect(sentBody(mock, 'POST /api/assistant/messages')).toEqual({ messages })
  })

  it('reads the configured status', async () => {
    stubFetch({ 'GET /api/assistant/status': { body: { configured: true } } })

    expect(await fetchAssistantStatus()).toEqual({ configured: true })
  })

  it('throws AssistantError carrying the server detail', async () => {
    stubFetch({
      'POST /api/assistant/messages': {
        body: { detail: 'the assistant is not configured' },
        status: 503,
      },
    })

    await expect(
      sendAssistantMessages([{ role: 'user', content: 'x' }]),
    ).rejects.toMatchObject({ status: 503, message: 'the assistant is not configured' })
  })
})
