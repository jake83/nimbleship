import { diffServices } from './diff'
import type { ServiceDeclaration } from './types'

function service(overrides: Partial<ServiceDeclaration>): ServiceDeclaration {
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

describe('diffServices', () => {
  it('reports no entries when nothing changed', () => {
    const services = [service({})]

    const diff = diffServices(services, services)

    expect(diff.entries).toEqual([])
    expect(diff.unchangedCount).toBe(1)
  })

  it('reports an added service', () => {
    const added = service({ code: 'DROPOUT-XL', tie_break_order: 2 })

    const diff = diffServices([service({})], [service({}), added])

    expect(diff.entries).toEqual([
      { kind: 'added', code: 'DROPOUT-XL', service: added },
    ])
  })

  it('reports a removed service', () => {
    const removed = service({ code: 'DROPOUT-XL', tie_break_order: 2 })

    const diff = diffServices([service({}), removed], [service({})])

    expect(diff.entries).toEqual([
      { kind: 'removed', code: 'DROPOUT-XL', service: removed },
    ])
  })

  it('reports field-level changes on a service present in both', () => {
    const before = service({})
    const after = service({ cost: '5.00', countries: ['GB', 'IE'] })

    const diff = diffServices([before], [after])

    expect(diff.entries).toEqual([
      {
        kind: 'changed',
        code: 'DROPOUT-STD',
        changes: [
          { field: 'cost', before: '4.50', after: '5.00' },
          { field: 'countries', before: 'GB', after: 'GB, IE' },
        ],
      },
    ])
    expect(diff.unchangedCount).toBe(0)
  })

  it('renders null and list values readably', () => {
    const before = service({ areas_served: null, max_girth_cm: null })
    const after = service({ areas_served: ['NI', 'HI'], max_girth_cm: '300' })

    const diff = diffServices([before], [after])

    expect(diff.entries).toEqual([
      {
        kind: 'changed',
        code: 'DROPOUT-STD',
        changes: [
          { field: 'areas_served', before: '(not set)', after: 'NI, HI' },
          { field: 'max_girth_cm', before: '(not set)', after: '300' },
        ],
      },
    ])
  })

  it('orders entries by service code, removed before added before changed', () => {
    const kept = service({})
    const diff = diffServices(
      [kept, service({ code: 'B-GONE', tie_break_order: 2 })],
      [
        { ...kept, cost: '9.00' },
        service({ code: 'A-NEW', tie_break_order: 3 }),
      ],
    )

    expect(diff.entries.map((e) => [e.kind, e.code])).toEqual([
      ['removed', 'B-GONE'],
      ['added', 'A-NEW'],
      ['changed', 'DROPOUT-STD'],
    ])
  })
})
