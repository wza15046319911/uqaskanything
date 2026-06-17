"""
build_kb_vocab.py — KB whole-corpus vocabulary (used by the answerability gate)
(matches step 1 of P0 in .claude/plans/kb-answerability.md)

Tokenize the full kb_chunks.text into a word set, written to data/kb/kb_vocab.txt (each line is "word\\tfrequency", sorted by frequency descending).
answerability.answerable() uses it to decide "whether an English entity is absent from the whole corpus". Run it whenever the KB is rebuilt (after kb_build),
otherwise the vocabulary goes stale after a corpus update and a real question about a new page is misjudged as fictional (a plan risk item).

Tokenizing: English uses answerability.EN_WORD (same as the query, so the vocabulary can match question words); a chunk containing Chinese
is additionally cut with jieba and added to the table (the current corpus is almost all English, only a handful of chunks are Chinese — they are added so the vocabulary is complete when the corpus contains Chinese pages later;
answerability currently does not do absence checks on Chinese, see that module's notes).

Usage (run from backend/):
    python -m app.pipelines.build_kb_vocab                 # default reads kb_chunks from the DB
    python -m app.pipelines.build_kb_vocab --chunks data/kb/chunks_all.jsonl   # offline read from JSONL
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
    """Read all text from DB kb_chunks (the authoritative corpus already loaded)."""
    with psycopg.connect(DSN) as conn:
        conn.read_only = True
        return [r[0] or "" for r in conn.execute("SELECT text FROM kb_chunks").fetchall()]


def _texts_from_file(path: Path) -> list[str]:
    """Read text from the chunks JSONL (offline, does not depend on the DB)."""
    out: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            out.append(json.loads(ln).get("text", "") or "")
    return out


def build_vocab(texts: list[str]) -> Counter:
    """List of texts -> word-frequency Counter (English via regex + texts containing Chinese go through jieba)."""
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
