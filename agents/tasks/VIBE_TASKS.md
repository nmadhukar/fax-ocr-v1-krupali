# Vibe Task Catalog

This is the repeatable task list for agent-driven execution in this repository.

## Task: VIBE-01 (Onboard And Baseline)

- Objective: establish local confidence before touching logic.
- Steps:
1. Read `AGENTS.md`.
2. Run quick baseline checks.
3. Confirm health endpoints if services are running.
- Commands:
  - `./scripts/vibe.sh review-fast`
  - `./scripts/vibe.ps1 review-fast`
  - Optional: `./scripts/vibe.sh smoke-api`

## Task: VIBE-10 (Ultra Deep Review)

- Objective: find logic bugs, flow gaps, and race conditions.
- Primary skill: `agents/skills/deep-review/SKILL.md`
- Steps:
1. Trace end-to-end flow for impacted module.
2. Identify invariant violations and fallback errors.
3. Produce severity-ranked findings with exact file references.
- Commands:
  - `./scripts/vibe.sh test-regression`
  - `./scripts/vibe.ps1 test-regression`

## Task: VIBE-20 (Fix Findings)

- Objective: patch identified issues with minimal blast radius.
- Primary skill: `agents/skills/fix-and-harden/SKILL.md`
- Steps:
1. Implement focused fix.
2. Add/update regression test.
3. Re-validate.
- Commands:
  - `./scripts/vibe.sh review-fast`
  - `./scripts/vibe.ps1 review-fast`

## Task: VIBE-30 (Functional Validation)

- Objective: prove the updated flow is stable.
- Primary skill: `agents/skills/functional-validation/SKILL.md`
- Steps:
1. Run fast gate.
2. Run full gate when change is broad/high-risk.
3. Record command outcomes.
- Commands:
  - Fast: `./scripts/vibe.sh review-fast` or `./scripts/vibe.ps1 review-fast`
  - Full: `./scripts/vibe.sh review-full` or `./scripts/vibe.ps1 review-full`

## Task: VIBE-40 (Docs And Handoff)

- Objective: make the project understandable for the next agent.
- Primary skill: `agents/skills/docs-sync/SKILL.md`
- Steps:
1. Sync docs changed by behavior updates.
2. Include validation evidence in handoff.
3. Capture residual risks and next actions.
