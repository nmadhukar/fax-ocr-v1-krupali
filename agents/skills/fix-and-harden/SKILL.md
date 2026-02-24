# Skill: Fix And Harden

## Goal

Implement targeted fixes that close the issue and prevent recurrence.

## When To Use

- "Fix all issues"
- "Patch and validate"
- "Resolve review findings"

## Workflow

1. Reproduce or reason from a concrete failing path.
2. Apply minimal code change preserving existing behavior elsewhere.
3. Add regression test for the exact bug class.
4. Re-run validation gates.
5. Summarize exact before/after behavior.

## Guardrails

- Do not silently broaden business logic.
- Keep compatibility with existing API contracts unless explicitly changing contract.
- Prefer deterministic tests over environment-sensitive tests.

## Required Commands

- `./scripts/vibe.sh test-regression` or `./scripts/vibe.ps1 test-regression`
- `./scripts/vibe.sh review-fast` or `./scripts/vibe.ps1 review-fast`

## Output Contract

1. Patched files.
2. New/updated tests.
3. Validation result.
