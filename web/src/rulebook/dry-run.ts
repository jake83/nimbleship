import type { DryRunOutcome } from '@/rulebook/types'

/** One sentence for result headers and the publish confirmation. */
export function dryRunSummary(outcome: DryRunOutcome): string {
  return `${outcome.changed} of ${outcome.total} orders would change service.`
}

/** Order numbers as users paste them: newline, space or comma separated. */
export function parseOrderNumbers(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((order) => order.trim())
    .filter((order) => order !== '')
}
