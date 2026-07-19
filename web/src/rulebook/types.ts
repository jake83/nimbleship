// Mirrors the response models in api/src/nimbleship/routers/rulebook.py.
// Decimal fields arrive as JSON strings (pydantic serialises Decimal to str).

/** A service declaration (ADR 0008 layer 1): facts on the carrier service. */
export interface ServiceDeclaration {
  code: string
  carrier: string
  name: string
  weight_min_kg: string
  weight_max_kg: string
  countries: string[]
  cost: string
  tie_break_order: number
  max_dimension_cm: string | null
  max_girth_cm: string | null
  areas_served: string[] | null
  areas_blocked: string[]
  propositions: string[]
  service_groups: string[]
  // Banded Delivery Cost/Charge structures (owned by other chunks); the
  // Rules UI carries them through untouched, never edits them.
  cost_bands: unknown[] | null
  charge_bands: unknown[] | null
}

export type VersionStatus = 'draft' | 'published'

export interface VersionSummary {
  version: number
  status: VersionStatus
  author: string
  description: string | null
  created_at: string
}

export interface VersionDetail extends VersionSummary {
  services: ServiceDeclaration[]
}

export interface ActiveRulebook {
  version: number
  services: ServiceDeclaration[]
}

export interface DryRunResult {
  order_number: string
  current_service: string | null
  draft_service: string | null
  changed: boolean
}

export interface DryRunOutcome {
  rulebook_version: number
  total: number
  changed: number
  results: DryRunResult[]
}
