import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Textarea } from '@/components/ui/textarea'
import { runDryRun } from '@/rulebook/api'
import { dryRunSummary, parseOrderNumbers } from '@/rulebook/dry-run'
import type { DryRunOutcome } from '@/rulebook/types'

type Mode = 'recent' | 'orders'

function serviceCell(code: string | null) {
  if (code === null) {
    return <span className="text-muted-foreground italic">no allocation</span>
  }
  return code
}

interface DryRunPanelProps {
  version: number
  /** Reports each outcome upward, e.g. for the publish confirmation. */
  onOutcome?: (outcome: DryRunOutcome) => void
}

/**
 * The ADR 0003 "test" step: replay historical orders through this version
 * and show what would change, before anything is published.
 */
export function DryRunPanel({ version, onOutcome }: DryRunPanelProps) {
  const [mode, setMode] = useState<Mode>('recent')
  const [limit, setLimit] = useState('50')
  const [orderNumbers, setOrderNumbers] = useState('')
  const [outcome, setOutcome] = useState<DryRunOutcome | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [running, setRunning] = useState(false)

  async function run() {
    setRunning(true)
    setError(null)
    try {
      const payload =
        mode === 'recent'
          ? { limit: Number(limit) }
          : { order_numbers: parseOrderNumbers(orderNumbers) }
      const result = await runDryRun(version, payload)
      setOutcome(result)
      onOutcome?.(result)
    } catch (caught) {
      setOutcome(null)
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setRunning(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Dry run</CardTitle>
        <CardDescription>
          Replay historical orders through version {version} and see which
          would be allocated differently. Nothing is changed.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex gap-2" role="group" aria-label="Orders to replay">
          <Button
            type="button"
            size="sm"
            variant={mode === 'recent' ? 'secondary' : 'ghost'}
            aria-pressed={mode === 'recent'}
            onClick={() => setMode('recent')}
          >
            Most recent orders
          </Button>
          <Button
            type="button"
            size="sm"
            variant={mode === 'orders' ? 'secondary' : 'ghost'}
            aria-pressed={mode === 'orders'}
            onClick={() => setMode('orders')}
          >
            Specific order numbers
          </Button>
        </div>

        {mode === 'recent' ? (
          <div className="flex max-w-xs flex-col gap-1.5">
            <Label htmlFor="dry-run-limit">Recent orders to replay</Label>
            <Input
              id="dry-run-limit"
              type="number"
              min={1}
              max={500}
              value={limit}
              onChange={(event) => setLimit(event.target.value)}
            />
          </div>
        ) : (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="dry-run-orders">Order numbers</Label>
            <Textarea
              id="dry-run-orders"
              placeholder="One per line, or comma-separated"
              value={orderNumbers}
              onChange={(event) => setOrderNumbers(event.target.value)}
            />
          </div>
        )}

        <div>
          <Button type="button" onClick={run} disabled={running}>
            {running ? 'Running...' : 'Run dry run'}
          </Button>
        </div>

        {error !== null && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}

        {outcome !== null && (
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">{dryRunSummary(outcome)}</p>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Order</TableHead>
                  <TableHead>Current service</TableHead>
                  <TableHead>Version {version} service</TableHead>
                  <TableHead>Outcome</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {outcome.results.map((result) => (
                  <TableRow
                    key={result.order_number}
                    data-changed={result.changed}
                    className={
                      result.changed
                        ? 'bg-amber-50 hover:bg-amber-100/80 dark:bg-amber-950/40 dark:hover:bg-amber-950/60'
                        : undefined
                    }
                  >
                    <TableCell className="font-mono">
                      {result.order_number}
                    </TableCell>
                    <TableCell>{serviceCell(result.current_service)}</TableCell>
                    <TableCell>{serviceCell(result.draft_service)}</TableCell>
                    <TableCell>
                      {result.changed ? (
                        <Badge variant="destructive">changed</Badge>
                      ) : (
                        <Badge variant="outline">unchanged</Badge>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
