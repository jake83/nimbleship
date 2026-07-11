# 6. shadcn/ui and Tailwind for the frontend

Date: 2026-07-11

## Status

Accepted

## Context

The old 3PL system hand-builds its Vue components and uses shadcn-style
charts only for graphs. NimbleShip's frontend is React/TypeScript (locked in
the founding session), which opens the question of the component approach:
hand-built again, a packaged component library (MUI, Ant, Mantine), or
shadcn/ui.

Much of NimbleShip will be built by AI coding agents working in parallel
worktrees, which rewards a component system that is idiomatic, widely known,
and lives as plain code in the repo rather than behind a package API.

## Decision

The frontend uses shadcn/ui components on Tailwind CSS with Radix
primitives. Components are copied into the repo and owned like any other
code; customisation happens by editing them, not by fighting a theme API.
Charts use the shadcn chart components (Recharts-based), continuing what the
old system already proved out.

## Consequences

- One styling system (Tailwind) across the app; no CSS-in-JS or bespoke
  design-system maintenance.
- Components are code we own: no breaking upgrades from a component vendor,
  at the cost of owning fixes ourselves.
- AI agents generate consistent UI, since shadcn/Tailwind is the pattern
  they are most fluent in.
- The liked 3PL design language is reproduced as a Tailwind theme
  (design tokens), not by porting hand-built components.
