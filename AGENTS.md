# Agent Operating Guide

This file defines the project-level rules and execution workflow for any coding agent working in this repository.

## 1) Global Rules

1. Keep tenant isolation intact.
   - Cross-tenant access must remain blocked **in all environments** (tenant isolation is always enforced regardless of `ENVIRONMENT`).
2. Preserve dedup behavior on upload.
   - Dedup identity is `(tenant_id, file_sha256)`.
   - Duplicate upload must return the existing job, not create a new one.
3. Preserve review workflow integrity.
   - Reviewer identity comes from auth user; body `reviewer_id` must not allow impersonation.
   - Claim expiry must be enforced before submit.
4. Keep composite page scoping strict.
   - Active segment pages must be honored through OCR aggregation, review packet pages, and label assignment.
5. Keep correction key normalization strict.
   - Corrections use canonical lowercase snake_case field keys.
   - Reject duplicate/unsupported keys.
6. Do not weaken HITL and analytics semantics.
   - Distinguish auto-finalized vs human-reviewed completions correctly.
7. Never bypass validation.
   - Every non-trivial code change must include functional validation commands and results.

## 2) Core Flow Map (Where Logic Lives)

- Ingress upload + dedup: `services/fax_ingress_api/api/v1/routes/faxes.py`
- Review claim/submit/packet: `services/fax_review_api/api/v1/routes/review.py`
- Template admin/sample upload: `services/fax_review_api/api/v1/routes/templates.py`
- Worker extraction stage: `workers/fax_processing_worker/tasks/stages/extraction.py`
- Worker finalization/review packet creation: `workers/fax_processing_worker/tasks/stages/finalization.py`
- Analytics aggregates: `libs/shared/db/repositories/analytics_repo.py`

## 3) Required Delivery Loop

1. Understand current behavior.
   - Read impacted routes/stages/repos first.
2. Implement minimally.
   - Change only what is required to fix the logic.
3. Add or update regression tests.
   - Protect the specific bug class you changed.
4. Run validation commands.
   - Use command set in section 5.
5. Sync docs when behavior/API changed.
   - Update README/API docs/technical notes as needed.

## 4) Subagent Skills And Task Catalog

- Skills index: `agents/skills/README.md`
- Skill definitions: `agents/skills/*/SKILL.md`
- Repeatable tasks: `agents/tasks/VIBE_TASKS.md`

## 5) Command Catalog (Repeatable + Executable)

Use these wrappers for consistent execution:

- PowerShell: `./scripts/vibe.ps1 <task>`
- Bash: `./scripts/vibe.sh <task>`

Supported tasks:

- `bootstrap` - install base + dev dependencies
- `format` - apply formatter/import sorting and safe auto-fixes
- `lint` - static style checks (ruff + black check + isort check)
- `typecheck` - mypy pass on core code
- `test-quick` - fast project test pass (`pytest -q`)
- `test-full` - verbose full test run
- `test-regression` - targeted regression suite
- `review-fast` - compile + quick tests
- `review-full` - lint + typecheck + full tests
- `smoke-api` - hit local health endpoints on ports 8001/8002/8003
- `smoke-docker` - check container status via `docker compose`

Equivalent Make targets are also available for review/testing:

- `make review-fast`
- `make review-full`
- `make test-regression`
- `make smoke-api`

## 6) Handoff Template (Use In Final Response)

1. What changed (files + behavior)
2. Why it changed (bug/risk addressed)
3. Validation run (exact commands + pass/fail)
4. Residual risks or follow-up actions
