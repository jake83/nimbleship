# 3. Versioned rule configurations with draft/test/publish

Date: 2026-07-11

## Status

Accepted

## Context

In the old 3PL system, allocation rules are edited live. The safety mechanisms
grew around that fragility: nightly JSON backups of the rule tables, a seeder
UI to restore them, and a separate update-log table. Users asked for "undo".

NimbleShip also plans AI-authored rule changes (a user asks the AI to adjust
routing), which makes "edits go straight to live" untenable: an AI-authored
change needs to be inspectable and testable before it affects real orders.

Alternatives considered:

- A simple undo stack over live edits (cheapest, no audit, no testing story).
- Version history with immediate publish on save.
- Immutable versions with a draft/test/publish workflow.

## Decision

Rule configurations (allocation/routing, and related rule-like config) are
versioned:

- Every save produces an immutable version with author, timestamp, and diff.
- The live system points at exactly one published version; rollback means
  repointing to an older version.
- Changes are made on a draft, which can be dry-run tested against historical
  orders, then published.
- The AI rule builder is just another author: it produces drafts that go
  through the identical test/publish gate as human edits.

## Consequences

- Unlimited undo, full audit trail; the seeder backups, restore UI, and
  update-log table have no successor - versioning subsumes them all.
- "Why did routing change?" is answerable: versions are diffable and
  attributable.
- Dry-run testing against historical orders becomes a first-class capability
  the rules engine must support (evaluate a draft version without side
  effects).
- Cost: the rules storage model is version-aware from day one, and the
  editing UI works on drafts rather than live state.
