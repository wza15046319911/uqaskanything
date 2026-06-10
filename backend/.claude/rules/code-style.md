# Code Style — Backend (Python / FastAPI)

Python 3.13. Layered package under `app/`. Keep the existing Chinese docstring
style — do not strip or translate it.

## Imports

- Use absolute imports through the package: `from app.services import qa`,
  `from app.core.config import DSN`. No relative imports (`from . import x`).
- Put a module in the layer that matches its job:
  - `app/api/` — FastAPI routers only (thin; delegate to services)
  - `app/services/` — business logic (retrieval, planner, simulator, ...)
  - `app/scrapers/` — UQ scrapers (CLI entry points)
  - `app/pipelines/` — DB build / embedding (CLI entry points)
  - `app/core/` — config and shared infra
- Every module starts with `from __future__ import annotations`.

## Config & paths

- DSN, data dir, and other shared config live ONLY in `app/core/config.py`.
  Import them — never re-declare `DSN = os.environ.get(...)` in a module.
- Data files live in `backend/data/`. CLI default paths point to `data/...`;
  run CLIs from the `backend/` directory.

## Naming

- snake_case for functions, variables, modules; PascalCase for classes.
- Private helpers are prefixed with `_` (`_offerings`, `_validate`).

## FastAPI

- One `APIRouter` per feature file in `app/api/`; `main.py` only assembles
  routers and starts the app.
- Do not swallow errors: return
  `JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=...)` with the
  real message. Surface skips/failures — never a silent default.

## Docstrings & comments

- The norm here is a module-level docstring describing purpose + 用法 (in
  Chinese). Keep it. Do not add per-line comments unless asked.

## Tests

- pytest under `tests/`, files named `test_*.py`.
- Pure-logic tests need no DB; integration tests connect to Postgres (:5433).
- Run `pytest` from `backend/` after any refactor.
