import { fromDeclaration, toDeclaration, validateDraft } from './draft'
import type { EditableService } from './draft'
import type { ServiceDeclaration } from './types'

function declaration(
  overrides: Partial<ServiceDeclaration>,
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

function editable(overrides: Partial<EditableService>): EditableService {
  return { ...fromDeclaration(declaration({})), ...overrides }
}

describe('fromDeclaration / toDeclaration', () => {
  it('round-trips a declaration unchanged', () => {
    const original = declaration({
      max_girth_cm: '300',
      areas_served: ['NI'],
      areas_blocked: ['HI'],
      propositions: ['next-day'],
    })

    expect(toDeclaration(fromDeclaration(original))).toEqual(original)
  })

  it('carries cost and charge bands through untouched', () => {
    const banded = declaration({
      cost_bands: [{ kind: 'weight', base: '1.00' }],
      charge_bands: [{ scope: 'all', base: '2.00' }],
    })

    expect(toDeclaration(fromDeclaration(banded))).toEqual(banded)
  })

  it('carries service groups through untouched (no form field yet)', () => {
    // The editor has no service_groups field, but a service's groups must survive a
    // round trip rather than being silently dropped on save.
    const grouped = declaration({ service_groups: ['ECONOMY', 'STANDARD'] })

    expect(toDeclaration(fromDeclaration(grouped))).toEqual(grouped)
  })

  it('parses comma-separated lists tolerantly', () => {
    const service = editable({ countries: ' gb, ie ,, FR ' })

    expect(toDeclaration(service).countries).toEqual(['GB', 'IE', 'FR'])
  })

  it('treats blank optional fields as not set', () => {
    const service = editable({
      maxDimensionCm: '',
      areasServed: '',
      areasBlocked: '',
      propositions: '',
    })

    const result = toDeclaration(service)
    expect(result.max_dimension_cm).toBeNull()
    expect(result.areas_served).toBeNull()
    expect(result.areas_blocked).toEqual([])
    expect(result.propositions).toEqual([])
  })
})

describe('validateDraft', () => {
  it('accepts a valid draft', () => {
    const validation = validateDraft([editable({})])

    expect(validation.valid).toBe(true)
    expect(validation.draftErrors).toEqual([])
  })

  it('rejects an empty draft', () => {
    const validation = validateDraft([])

    expect(validation.valid).toBe(false)
    expect(validation.draftErrors[0]).toMatch(/at least one service/i)
  })

  it('requires code, carrier and name', () => {
    const validation = validateDraft([
      editable({ code: '', carrier: ' ', name: '' }),
    ])

    expect(validation.valid).toBe(false)
    const errors = validation.serviceErrors[0]!
    expect(errors.code).toMatch(/required/i)
    expect(errors.carrier).toMatch(/required/i)
    expect(errors.name).toMatch(/required/i)
  })

  it('rejects non-numeric and negative numbers', () => {
    const validation = validateDraft([
      editable({ weightMinKg: 'abc', cost: '-1', maxGirthCm: 'x' }),
    ])

    const errors = validation.serviceErrors[0]!
    expect(errors.weightMinKg).toMatch(/number/i)
    expect(errors.cost).toMatch(/negative/i)
    expect(errors.maxGirthCm).toMatch(/number/i)
  })

  it('rejects a weight range whose minimum exceeds its maximum', () => {
    const validation = validateDraft([
      editable({ weightMinKg: '31', weightMaxKg: '30' }),
    ])

    expect(validation.serviceErrors[0]!.weightMaxKg).toMatch(/minimum/i)
  })

  it('requires at least one country', () => {
    const validation = validateDraft([editable({ countries: ' , ' })])

    expect(validation.serviceErrors[0]!.countries).toMatch(/country/i)
  })

  it('requires a whole-number tie-break order', () => {
    const validation = validateDraft([editable({ tieBreakOrder: '1.5' })])

    expect(validation.serviceErrors[0]!.tieBreakOrder).toMatch(/whole number/i)
  })

  it('rejects duplicate service codes across the draft', () => {
    const validation = validateDraft([
      editable({}),
      editable({ tieBreakOrder: '2' }),
    ])

    expect(validation.valid).toBe(false)
    expect(validation.serviceErrors[1]!.code).toMatch(/duplicate/i)
  })

  it('rejects duplicate tie-break orders across the draft', () => {
    const validation = validateDraft([
      editable({}),
      editable({ code: 'DROPOUT-XL' }),
    ])

    expect(validation.valid).toBe(false)
    expect(validation.serviceErrors[1]!.tieBreakOrder).toMatch(/duplicate/i)
  })
})
