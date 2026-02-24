# Skills Index

These skills are reusable playbooks for subagents.

## Available Skills

- `agents/skills/deep-review/SKILL.md`
  - Ultra-deep functional review across API routes, worker stages, and repository semantics.
- `agents/skills/fix-and-harden/SKILL.md`
  - Convert findings into minimal patches plus regression protection.
- `agents/skills/functional-validation/SKILL.md`
  - Execute deterministic validation gates and report outcomes.
- `agents/skills/docs-sync/SKILL.md`
  - Keep API and developer docs aligned with behavior changes.

## Usage

1. Choose one primary skill based on objective.
2. Apply its workflow exactly.
3. Use command wrappers from `scripts/vibe.ps1` or `scripts/vibe.sh`.
4. When code changes behavior, always run `functional-validation` and `docs-sync`.
