import type { ServiceDeclaration } from '@/rulebook/types'

/** One field that differs between two versions of the same service. */
export interface FieldChange {
  field: string
  before: string
  after: string
}

export type DiffEntry =
  | { kind: 'added'; code: string; service: ServiceDeclaration }
  | { kind: 'removed'; code: string; service: ServiceDeclaration }
  | { kind: 'changed'; code: string; changes: FieldChange[] }

export interface ServicesDiff {
  entries: DiffEntry[]
  unchangedCount: number
}

/** Render a declaration value for the diff view. */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '(not set)'
  if (Array.isArray(value)) {
    if (value.length === 0) return '(none)'
    return value.map((item) => formatValue(item)).join(', ')
  }
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function changedFields(
  before: ServiceDeclaration,
  after: ServiceDeclaration,
): FieldChange[] {
  const beforeRecord: Record<string, unknown> = { ...before }
  const afterRecord: Record<string, unknown> = { ...after }
  const fields = [
    ...new Set([...Object.keys(beforeRecord), ...Object.keys(afterRecord)]),
  ].sort()
  const changes: FieldChange[] = []
  for (const field of fields) {
    const beforeValue = beforeRecord[field]
    const afterValue = afterRecord[field]
    if (JSON.stringify(beforeValue) !== JSON.stringify(afterValue)) {
      changes.push({
        field,
        before: formatValue(beforeValue),
        after: formatValue(afterValue),
      })
    }
  }
  return changes
}

/**
 * Client-side diff of two service lists, matched by service code
 * (the identity a version's services are keyed on).
 */
export function diffServices(
  previous: ServiceDeclaration[],
  current: ServiceDeclaration[],
): ServicesDiff {
  const previousByCode = new Map(previous.map((s) => [s.code, s]))
  const currentByCode = new Map(current.map((s) => [s.code, s]))

  const removed: DiffEntry[] = previous
    .filter((s) => !currentByCode.has(s.code))
    .map((s) => ({ kind: 'removed', code: s.code, service: s }))
  const added: DiffEntry[] = current
    .filter((s) => !previousByCode.has(s.code))
    .map((s) => ({ kind: 'added', code: s.code, service: s }))

  const changed: DiffEntry[] = []
  let unchangedCount = 0
  for (const service of current) {
    const before = previousByCode.get(service.code)
    if (!before) continue
    const changes = changedFields(before, service)
    if (changes.length === 0) {
      unchangedCount += 1
    } else {
      changed.push({ kind: 'changed', code: service.code, changes })
    }
  }

  const byCode = (a: DiffEntry, b: DiffEntry) => a.code.localeCompare(b.code)
  return {
    entries: [
      ...removed.sort(byCode),
      ...added.sort(byCode),
      ...changed.sort(byCode),
    ],
    unchangedCount,
  }
}
