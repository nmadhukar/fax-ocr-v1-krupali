# Skill: Functional Validation

## Goal

Ensure changed behavior is functionally correct and non-regressive.

## Validation Levels

1. `fast`
   - Compile + quick tests.
2. `standard`
   - Lint + quick tests.
3. `full`
   - Lint + typecheck + full test run + smoke checks.

## Command Set

- Fast:
  - `./scripts/vibe.sh review-fast`
  - `./scripts/vibe.ps1 review-fast`
- Standard:
  - `./scripts/vibe.sh lint && ./scripts/vibe.sh test-quick`
  - `./scripts/vibe.ps1 lint; ./scripts/vibe.ps1 test-quick`
- Full:
  - `./scripts/vibe.sh review-full`
  - `./scripts/vibe.ps1 review-full`
- Optional runtime smoke:
  - `./scripts/vibe.sh smoke-api`
  - `./scripts/vibe.ps1 smoke-api`

## Output Contract

Report exact commands and pass/fail outcomes. If something is skipped, say why.
