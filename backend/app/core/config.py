"""Central config: DSN, data directory, S2 offering codes.

All runtime modules read from here, to avoid repeating DSN and similar values.
"""
from __future__ import annotations
import os
import pathlib

# this file is at backend/app/core/config.py; go up 3 levels to the backend/ root
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_ROOT / "data"

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")

# S2 offering codes: appearing on the 2026:2 search page means the course runs in S2 (see docs/s2_progress.md). Empty set if the file is missing.
_S2_FILE = DATA_DIR / "s2_course_codes.txt"
S2_CODES = set(_S2_FILE.read_text().split()) if _S2_FILE.exists() else set()
