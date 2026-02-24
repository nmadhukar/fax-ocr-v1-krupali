# Skill: Docs Sync

## Goal

Keep project docs aligned with real behavior after code changes.

## When To Use

- API request/response behavior changed.
- Business logic flow changed.
- New commands/tasks were added for developers or agents.

## Workflow

1. Identify impacted docs.
   - `README.md`, `API_REFERENCE.md`, `README_TECHNICAL.md`, `AGENTS.md`.
2. Update examples and command snippets.
3. Remove stale instructions that no longer match code.
4. Keep edits concise and verifiable.

## Minimum Required Update

- If only internal engineering workflow changed, update `AGENTS.md` and task catalog.
- If endpoint behavior changed, update API docs in the same PR.

## Output Contract

List each doc updated and what was synced.
