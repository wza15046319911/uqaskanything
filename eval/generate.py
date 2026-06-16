"""generate.py — 调被评测后端 /api/ask,把每道题的「答案 + 检索上下文」落成 RAGAS 样本。

走 HTTP 调 backend(像前端那样),不 import backend,保持 eval 与后端进程/依赖解耦。
contexts 按 mode 取(与 qa.run 返回结构对齐):
  - kb               -> chunks[].text
  - course_detail    -> 单门课结构化拼文本
  - filter/semantic/hybrid -> courses[] 每行拼文本
  - program/empty    -> 无检索上下文(确定性答案,RAGAS 不适用)-> contexts 置空,下游会跳过并计数

用法(后端需在 BACKEND_URL 上跑;从仓库根):
    python eval/generate.py
    python eval/generate.py --questions eval/data/questions.jsonl --out eval/data/generated.jsonl
"""
from __future__ import annotations

import os
import json
import argparse
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HERE = Path(__file__).resolve().parent
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8077").rstrip("/")


def contexts_from(res: dict) -> list[str]:
    """直接用后端 gen_context:与生产实际喂给 LLM 的检索上下文同源,零漂移。"""
    return [c for c in (res.get("gen_context") or []) if c]


def ask(question: str) -> dict:
    r = requests.post(f"{BACKEND_URL}/api/ask", json={"question": question}, timeout=120)
    r.raise_for_status()
    res = r.json()
    if "error" in res:
        raise RuntimeError(f"后端返回错误:{res['error']}")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(HERE / "data" / "questions.jsonl"))
    ap.add_argument("--out", default=str(HERE / "data" / "generated.jsonl"))
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.questions).read_text(encoding="utf-8").splitlines() if l.strip()]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped: list[str] = []
    failed: list[str] = []
    with out_path.open("w", encoding="utf-8") as f:
        for row in rows:
            q = row["q"]
            try:
                res = ask(q)
            except Exception as e:
                failed.append(f"{q!r}: {type(e).__name__}: {e}")
                continue
            ctxs = contexts_from(res)
            sample = {
                "q": q,
                "mode": res.get("mode"),
                "answer": res.get("answer") or "",
                "contexts": ctxs,
            }
            if "reference" in row:
                sample["reference"] = row["reference"]
            for k in ("tier", "refuse", "broad"):   # 透传题面标注,供下游分层 / 拒答判定
                if k in row:
                    sample[k] = row[k]
            if not ctxs:
                skipped.append(f"{q!r} (mode={res.get('mode')}: 无检索上下文)")
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
            written += 1

    print(f"写出 {written} 条 -> {out_path}")
    if skipped:
        print(f"无上下文(RAGAS 将跳过)共 {len(skipped)} 条:")
        for s in skipped:
            print(f"  - {s}")
    if failed:
        print(f"后端调用失败 {len(failed)} 条:")
        for s in failed:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
