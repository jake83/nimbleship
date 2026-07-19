import type { ServiceDeclaration } from '@/rulebook/types'

/**
 * A service declaration as the draft editor form holds it: every field a
 * string, lists comma-separated, blanks meaning "not set". Cost/charge bands
 * are owned by other tooling and pass through the editor untouched.
 */
export interface EditableService {
  code: string
  carrier: string
  name: string
  weightMinKg: string
  weightMaxKg: string
  countries: string
  cost: string
  tieBreakOrder: string
  maxDimensionCm: string
  maxGirthCm: string
  /** Blank = anywhere within the allowed countries (areas_served: null). */
  areasServed: string
  areasBlocked: string
  propositions: string
  /** Carried through untouched: the manual editor has no field for it yet, but a
   * service's groups must survive a round trip (they filter legacy orders). */
  serviceGroups: string[]
  costBands: unknown[] | null
  chargeBands: unknown[] | null
}

export function fromDeclaration(service: ServiceDeclaration): EditableService {
  return {
    code: service.code,
    carrier: service.carrier,
    name: service.name,
    weightMinKg: service.weight_min_kg,
    weightMaxKg: service.weight_max_kg,
    countries: service.countries.join(', '),
    cost: service.cost,
    tieBreakOrder: String(service.tie_break_order),
    maxDimensionCm: service.max_dimension_cm ?? '',
    maxGirthCm: service.max_girth_cm ?? '',
    areasServed: service.areas_served?.join(', ') ?? '',
    areasBlocked: service.areas_blocked.join(', '),
    propositions: service.propositions.join(', '),
    serviceGroups: service.service_groups,
    costBands: service.cost_bands,
    chargeBands: service.charge_bands,
  }
}

function parseList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim().toUpperCase())
    .filter((item) => item !== '')
}

function parseCaseSensitiveList(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item !== '')
}

export function toDeclaration(service: EditableService): ServiceDeclaration {
  return {
    code: service.code.trim(),
    carrier: service.carrier.trim(),
    name: service.name.trim(),
    weight_min_kg: service.weightMinKg.trim(),
    weight_max_kg: service.weightMaxKg.trim(),
    countries: parseList(service.countries),
    cost: service.cost.trim(),
    tie_break_order: Number(service.tieBreakOrder),
    max_dimension_cm: service.maxDimensionCm.trim() || null,
    max_girth_cm: service.maxGirthCm.trim() || null,
    areas_served:
      service.areasServed.trim() === ''
        ? null
        : parseCaseSensitiveList(service.areasServed),
    areas_blocked: parseCaseSensitiveList(service.areasBlocked),
    propositions: parseCaseSensitiveList(service.propositions),
    service_groups: service.serviceGroups,
    cost_bands: service.costBands,
    charge_bands: service.chargeBands,
  }
}

export type ServiceFieldErrors = Partial<
  Record<keyof EditableService, string>
>

export interface DraftValidation {
  valid: boolean
  /** Draft-level problems (e.g. no services at all). */
  draftErrors: string[]
  /** Per-service field errors, indexed like the services array. */
  serviceErrors: ServiceFieldErrors[]
}

const DECIMAL_PATTERN = /^\d+(\.\d+)?$/
const INTEGER_PATTERN = /^\d+$/

function decimalError(value: string, required: boolean): string | undefined {
  const trimmed = value.trim()
  if (trimmed === '') return required ? 'Required' : undefined
  if (trimmed.startsWith('-')) return 'Must not be negative'
  if (!DECIMAL_PATTERN.test(trimmed)) return 'Must be a number'
  return undefined
}

function validateService(service: EditableService): ServiceFieldErrors {
  const errors: ServiceFieldErrors = {}
  if (service.code.trim() === '') errors.code = 'Required'
  if (service.carrier.trim() === '') errors.carrier = 'Required'
  if (service.name.trim() === '') errors.name = 'Required'

  const weightMin = decimalError(service.weightMinKg, true)
  if (weightMin) errors.weightMinKg = weightMin
  const weightMax = decimalError(service.weightMaxKg, true)
  if (weightMax) {
    errors.weightMaxKg = weightMax
  } else if (
    !weightMin &&
    Number(service.weightMaxKg) < Number(service.weightMinKg)
  ) {
    errors.weightMaxKg = 'Must be at least the minimum weight'
  }

  const cost = decimalError(service.cost, true)
  if (cost) errors.cost = cost
  const maxDimension = decimalError(service.maxDimensionCm, false)
  if (maxDimension) errors.maxDimensionCm = maxDimension
  const maxGirth = decimalError(service.maxGirthCm, false)
  if (maxGirth) errors.maxGirthCm = maxGirth

  if (parseList(service.countries).length === 0) {
    errors.countries = 'At least one country code is required'
  }

  const tieBreak = service.tieBreakOrder.trim()
  if (tieBreak === '') {
    errors.tieBreakOrder = 'Required'
  } else if (!INTEGER_PATTERN.test(tieBreak)) {
    errors.tieBreakOrder = 'Must be a whole number'
  }

  return errors
}

/**
 * Client-side mirror of the Rulebook model's rules (required facts, unique
 * codes, unique tie-break orders) so authors see problems while typing;
 * the API re-validates on save and stays the authority.
 */
export function validateDraft(services: EditableService[]): DraftValidation {
  const draftErrors: string[] = []
  if (services.length === 0) {
    draftErrors.push(
      'A rulebook needs at least one service - an empty rulebook would block every order.',
    )
  }

  const serviceErrors = services.map(validateService)

  const seenCodes = new Set<string>()
  const seenOrders = new Set<string>()
  services.forEach((service, index) => {
    const code = service.code.trim()
    if (code !== '' && !serviceErrors[index]!.code) {
      if (seenCodes.has(code)) {
        serviceErrors[index]!.code = `Duplicate service code: ${code}`
      }
      seenCodes.add(code)
    }
    const order = service.tieBreakOrder.trim()
    if (order !== '' && !serviceErrors[index]!.tieBreakOrder) {
      if (seenOrders.has(order)) {
        serviceErrors[index]!.tieBreakOrder = `Duplicate tie-break order: ${order}`
      }
      seenOrders.add(order)
    }
  })

  const valid =
    draftErrors.length === 0 &&
    serviceErrors.every((errors) => Object.keys(errors).length === 0)
  return { valid, draftErrors, serviceErrors }
}
