# Skill: Deep Review

## Goal

Find logic/flow bugs and behavioral regressions with high confidence.

## When To Use

- "Ultra deep review"
- "Find all logic issues"
- "Validate business flow end-to-end"

## Workflow

1. Map impacted flow edges.
   - Ingress -> worker stages -> review -> analytics.
2. Trace invariants and state transitions.
   - Status moves, page scoping, correction normalization, dedup behavior.
3. Inspect failure modes.
   - Race conditions, stale locks, empty-selection behavior, fallback mistakes.
4. Classify findings by severity.
   - `critical`, `high`, `medium`, `low`.
5. Provide actionable evidence.
   - File path + exact line + why + likely impact + reproduction sketch.

## Required Commands

- Fast baseline:
  - `./scripts/vibe.sh review-fast` or `./scripts/vibe.ps1 review-fast`
- Regression focus:
  - `./scripts/vibe.sh test-regression` or `./scripts/vibe.ps1 test-regression`

## Output Contract

1. Findings first, ordered by severity.
2. Open questions/assumptions.
3. Optional quick summary last.
