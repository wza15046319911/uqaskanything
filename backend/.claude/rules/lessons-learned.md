# Lessons Learned — Backend

Practices learned from this codebase. Follow these to avoid repeating mistakes.

## Centralize Config, Don't Repeat It

**Problem**: The same `DSN = os.environ.get("DATABASE_URL", ...)` was declared in
12 files.

**Solution**: Define shared config once in `app/core/config.py` and import it.

**Key points**: One authority for DSN / data dir / S2 codes. If you find yourself
running a sed for the same line across many files, it should be centralized.

## Keep the Layered Import Direction

**Problem**: Flat `import qa` breaks the moment modules move into packages.

**Solution**: Always `from app.<layer> import <module>`. New code goes in the
layer matching its role.

**Key points**: api → services → core; CLIs live in scrapers / pipelines.
Routers stay thin and call into services.

## Data Paths Are Relative to `backend/`

**Problem**: CLI defaults like `default="courses.jsonl"` break after data moved
into `data/`.

**Solution**: Data lives in `backend/data/`; CLI defaults point to `data/...`,
and CLIs are run from `backend/`.

## Never Swallow Errors (matches the global rule)

**Problem**: Returning a default on failure hides real problems; skipped records
vanish into logs.

**Solution**: Raise or return the error with its message; report skips with
counts. If you cannot confirm 100% success, say so explicitly.

## Deterministic Logic in Code, LLM Only for Language

**Problem**: Letting the model decide routing / thresholds that must be stable.

**Solution**: Keep routing, guards, and validation in code (see the planner route
guards and sim_advise dual guardrails). The LLM only classifies, drafts, or
explains — it never makes the deterministic decision.

## Verify After a Refactor

**Problem**: Claiming a refactor is safe without running anything.

**Solution**: Run `pytest` (41 tests) after any import/structure change, and
smoke-test `from app.main import app` to catch broken imports early.

## Read Before Editing; Follow the Existing Style

**Problem**: Adding code without reading the module and its import graph first.

**Solution**: Read the file and its imports before changing it. Match the Chinese
docstring style and existing patterns. Don't introduce a second convention next
to an existing one.
