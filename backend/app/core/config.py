"""集中配置:DSN、数据目录、S2 开课码。

所有运行时模块从这里取,避免 DSN 等散落重复。
"""
from __future__ import annotations
import os
import pathlib

# 本文件位于 backend/app/core/config.py，上溯 3 层到 backend/ 根
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = BACKEND_ROOT / "data"

DSN = os.environ.get("DATABASE_URL", "postgresql://postgres:uqrag@localhost:5433/uq_courses")

# S2 开课码:出现在 2026:2 搜索页即代表该课 S2 开（见 docs/s2_progress.md）。文件缺则空集。
_S2_FILE = DATA_DIR / "s2_course_codes.txt"
S2_CODES = set(_S2_FILE.read_text().split()) if _S2_FILE.exists() else set()
