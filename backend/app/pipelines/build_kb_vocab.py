"""
build_kb_vocab.py — KB 全语料词表(answerability 门用)
(对应 .claude/plans/kb-answerability.md P0 第 1 步)

把全量 kb_chunks.text 分词成词集,写 data/kb/kb_vocab.txt(每行「词\\t词频」,频次降序)。
answerability.answerable() 用它判「英文实体在全语料是否缺席」。随 KB 重建跑(kb_build 之后),
否则语料更新了词表过期,会把新页里的真问题误判成虚构(plan 风险条)。

分词:英文用 answerability.EN_WORD(与查询同口径,保证词表能命中问题词);含中文的 chunk
额外用 jieba 切词收进表(当前语料几乎全英文,中文仅个位数 chunk——收进来是为语料将来含中文页
时词表完整,answerability 当前不对中文做缺席判定,见该模块说明)。

用法(从 backend/ 跑):
    python -m app.pipelines.build_kb_vocab                 # 默认读 DB 的 kb_chunks
    python -m app.pipelines.build_kb_vocab --chunks data/kb/chunks_all.jsonl   # 离线从 JSONL 读
"""
from __future__ import annotations
import re
import json
import argparse
from pathlib import Path
from collections import Counter

import psycopg

from app.core.config import DSN, DATA_DIR
from app.services.answerability import EN_WORD

CJK = re.compile(r"[一-鿿]")


def _texts_from_db() -> list[str]:
    """从 DB kb_chunks 读全部 text(已灌库的权威语料)。"""
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        return [r[0] or "" for r in conn.execute("SELECT text FROM kb_chunks").fetchall()]


def _texts_from_file(path: Path) -> list[str]:
    """从 chunks JSONL 读 text(离线、不依赖 DB)。"""
    out: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln).get("text", "") or "")
    return out


def build_vocab(texts: list[str]) -> Counter:
    """文本列表 -> 词频 Counter(英文 regex + 含中文文本走 jieba)。"""
    freq: Counter = Counter()
    jieba_cut = None
    for t in texts:
        freq.update(EN_WORD.findall(t.lower()))
        if CJK.search(t):
            if jieba_cut is None:
                import jieba
                jieba_cut = jieba.cut
            for w in jieba_cut(t):
                w = w.strip()
                if CJK.search(w):
                    freq[w] += 1
    return freq


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default=None,
                    help="离线从 chunks JSONL 读;缺省读 DB kb_chunks")
    ap.add_argument("--out", default=str(DATA_DIR / "kb" / "kb_vocab.txt"))
    args = ap.parse_args()

    if args.chunks:
        src = Path(args.chunks)
        if not src.exists():
            ap.error(f"找不到 {src}")
        texts = _texts_from_file(src)
        origin = str(src)
    else:
        texts = _texts_from_db()
        origin = f"DB kb_chunks(DSN={DSN})"

    if not texts:
        ap.error(f"语料为空:{origin};先跑 kb_build / kb_parse")

    freq = build_vocab(texts)
    cjk_words = sum(1 for w in freq if CJK.search(w))
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as f:
        for word, n in freq.most_common():
            f.write(f"{word}\t{n}\n")

    print(f"语料:{len(texts)} chunk(来源 {origin})")
    print(f"词表:{len(freq)} 词(其中中文 {cjk_words})-> {out}")
    print(f"高频样例:{[w for w, _ in freq.most_common(8)]}")


if __name__ == "__main__":
    main()
