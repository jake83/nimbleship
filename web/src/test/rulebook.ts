import { vi } from 'vitest'

import type { ServiceDeclaration, VersionDetail } from '@/rulebook/types'

export function service(
  overrides: Partial<ServiceDeclaration> = {},
): ServiceDeclaration {
  return {
    code: 'DROPOUT-STD',
    carrier: 'dropout',
    name: 'Drop Out Standard',
    weight_min_kg: '0',
    weight_max_kg: '30',
    countries: ['GB'],
    cost: '4.50',
    tie_break_order: 1,
    max_dimension_cm: null,
    max_girth_cm: null,
    areas_served: null,
    areas_blocked: [],
    propositions: [],
    service_groups: [],
    cost_bands: null,
    charge_bands: null,
    ...overrides,
  }
}

export function versionDetail(
  overrides: Partial<VersionDetail> = {},
): VersionDetail {
  return {
    version: 1,
    status: 'published',
    author: 'seed',
    description: null,
    created_at: '2026-07-10T09:00:00Z',
    services: [service()],
    ...overrides,
  }
}

interface StubResponse {
  body: unknown
  status?: number
}

/**
 * Stub global fetch with a "METHOD url" -> response map. Unmocked calls
 * fail loudly so tests never silently hit a real API.
 */
export function stubFetch(routes: Record<string, StubResponse>) {
  const mock = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const key = `${init?.method ?? 'GET'} ${String(input)}`
      const route = routes[key]
      if (route === undefined) {
        throw new Error(`unmocked fetch: ${key}`)
      }
      return new Response(JSON.stringify(route.body), {
        status: route.status ?? 200,
        headers: { 'Content-Type': 'application/json' },
      })
    },
  )
  vi.stubGlobal('fetch', mock)
  return mock
}

/** The parsed JSON body of the first call to `key`, from a stubFetch mock. */
export function sentBody(
  mock: ReturnType<typeof stubFetch>,
  key: string,
): unknown {
  const call = mock.mock.calls.find(
    ([input, init]) => `${init?.method ?? 'GET'} ${String(input)}` === key,
  )
  if (!call) throw new Error(`no fetch call matched: ${key}`)
  return JSON.parse(String(call[1]?.body))
}
