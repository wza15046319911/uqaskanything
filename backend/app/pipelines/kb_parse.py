"""
kb_parse.py — knowledge base stage three (3c general article parser) + stage four quality stats
(maps to plan.md sections 3c / 4 / 1.5)

Read the raw HTML pointed to by fetched.jsonl, use trafilatura to extract the body (markdown output
keeps h2/h3), split by headings, prefix each chunk with the breadcrumb "page title > h2 > h3", keep
it 300-800 tokens, hard-split overlong ones with ~50 token overlap. Produces:
  - data/kb/chunks.jsonl (for stage five embed loading)
  - a quality report printed to the terminal (parse success rate, body length, token distribution,
    <200-char pages, sampling)

Tokens are approximated by characters (English ≈ 4 char/token), only a rough filter for splitting and
stats, not exact.

Usage (run from backend/):
    python -m app.pipelines.kb_parse
    python -m app.pipelines.kb_parse --sample 8       # print a few more chunks for manual reading
"""
from __future__ import annotations
import re
import json
import logging
import argparse
import statistics
from pathlib import Path

import trafilatura
from trafilatura import extract, extract_metadata

from app.core.config import DATA_DIR

logging.getLogger("trafilatura").setLevel(logging.CRITICAL)

CHARS_PER_TOKEN = 4
SHORT_BODY_CHARS = 200          # body shorter than this -> likely JS-rendered / parse failure, flag it
MIN_CHUNK_TOKENS = 30           # chunks below this are marked short (over-fragmentation signal)


def _approx_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def _split_long(text: str, max_tok: int, overlap_tok: int) -> list[str]:
    """Hard-split overlong text by character window, keeping overlap_tok overlap."""
    if _approx_tokens(text) <= max_tok:
        return [text]
    win = max_tok * CHARS_PER_TOKEN
    step = (max_tok - overlap_tok) * CHARS_PER_TOKEN
    out = []
    i = 0
    while i < len(text):
        out.append(text[i:i + win].strip())
        i += step
    return [c for c in out if c]


def _linearize_tables(md: str) -> str:
    """markdown pipe table -> plain text with each row comma-joined.
    Remove |---| separator rows and `|` noise so table rows can be recalled correctly by embedding
    (pipe tables embed poorly)."""
    out: list[str] = []
    for line in md.split("\n"):
        s = line.strip()
        if re.match(r"^\|[\s:|-]+\|$", s):          # |---|---| separator row
            continue
        if s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            cells = [c for c in cells if c and c != "--"]
            if cells:
                out.append(", ".join(cells))
        else:
            out.append(line)
    return "\n".join(out)


def _pack(sections: list[tuple[str, str, str]],
          max_tok: int, overlap_tok: int) -> list[tuple[str, str, str]]:
    """Greedily merge adjacent small sections up to near max_tok; if a single section is overlong, flush first then hard-split.
    The breadcrumb takes the h2/h3 of the packed block's starting section (the block may span several subheadings, the cost of merging)."""
    packed: list[tuple[str, str, str]] = []
    cur = ""
    cur_h2 = cur_h3 = ""
    for h2, h3, body in sections:
        if _approx_tokens(body) > max_tok:
            if cur:
                packed.append((cur_h2, cur_h3, cur))
                cur = ""
            for piece in _split_long(body, max_tok, overlap_tok):
                packed.append((h2, h3, piece))
            continue
        if not cur:
            cur, cur_h2, cur_h3 = body, h2, h3
        elif _approx_tokens(cur) + _approx_tokens(body) <= max_tok:
            cur += "\n\n" + body
        else:
            packed.append((cur_h2, cur_h3, cur))
            cur, cur_h2, cur_h3 = body, h2, h3
    if cur:
        packed.append((cur_h2, cur_h3, cur))
    return packed


def sections_from_markdown(md: str) -> list[tuple[str, str, str]]:
    """markdown body -> [(h2, h3, body), ...], split into sections by ## / ### headings."""
    h2 = h3 = ""
    buf: list[str] = []
    out: list[tuple[str, str, str]] = []

    def flush():
        body = "\n".join(buf).strip()
        if body:
            out.append((h2, h3, body))
        buf.clear()

    for line in md.splitlines():
        m3 = re.match(r"^###\s+(.*)", line)
        m2 = re.match(r"^##\s+(.*)", line)
        m1 = re.match(r"^#\s+(.*)", line)
        if m2 or m1:
            flush()
            h2 = (m2 or m1).group(1).strip()
            h3 = ""
        elif m3:
            flush()
            h3 = m3.group(1).strip()
        else:
            buf.append(line)
    flush()
    return out


def parse_page(rec: dict, max_tok: int, overlap_tok: int,
               doc_type: str = "article") -> tuple[list[dict], int]:
    """Single page raw HTML -> (chunks, body_chars). body_chars=0 means extraction failed."""
    html_path = DATA_DIR.parent / rec["html_path"]
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    md = extract(html, output_format="markdown",
                 include_comments=False, include_tables=True) or ""
    meta = extract_metadata(html)
    title = (meta.title if meta and meta.title else "").strip()
    body_chars = len(md)
    if not md:
        return [], 0

    md = _linearize_tables(md)
    secs = sections_from_markdown(md)
    if not secs:                      # no headings: treat the whole page body as one section
        secs = [("", "", md)]

    chunks: list[dict] = []
    for h2, h3, piece in _pack(secs, max_tok, overlap_tok):
        if _approx_tokens(piece) < 10:        # pure symbol / blank-line noise, drop it
            continue
        crumb_parts: list[str] = []
        for x in (title, h2, h3):
            if x and (not crumb_parts or crumb_parts[-1] != x):
                crumb_parts.append(x)
        crumb = " > ".join(crumb_parts)
        text = f"{crumb}\n\n{piece}" if crumb else piece
        tok = _approx_tokens(piece)
        chunks.append({
                "id": f"{rec['url_hash']}-{len(chunks)}",
                "url": rec["url"],
                "domain": rec["domain"],
                "type": doc_type,
                "source_type": f"kb_{doc_type}",
                "page_title": title,
                "breadcrumb": crumb,
                "h2": h2,
                "h3": h3,
                "text": text,
                "char_len": len(piece),
                "approx_tokens": tok,
                "short": tok < MIN_CHUNK_TOKENS,
                "fetched_at": rec.get("fetched_at", ""),
                "lastmod": rec.get("lastmod", ""),
            })
    return chunks, body_chars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=str(DATA_DIR / "kb" / "fetched.jsonl"),
                    help="kb_fetch 产出的抓取记录")
    ap.add_argument("--out", default=str(DATA_DIR / "kb" / "chunks.jsonl"),
                    help="chunks 输出")
    ap.add_argument("--max-tok", type=int, default=800, help="chunk token 上限(超则硬切)")
    ap.add_argument("--overlap-tok", type=int, default=50, help="硬切重叠 token")
    ap.add_argument("--sample", type=int, default=5, help="人工抽读的 chunk 数")
    ap.add_argument("--doc-type", default="article", help="chunk 类型标签:article / faq")
    args = ap.parse_args()

    manifest = Path(args.manifest)
    if not manifest.exists():
        ap.error(f"找不到 {manifest};先跑 kb_fetch")

    recs = [json.loads(ln) for ln in manifest.read_text(encoding="utf-8").splitlines() if ln.strip()]
    all_chunks: list[dict] = []
    body_lens: list[int] = []
    failed: list[str] = []       # extracted an empty body
    short_pages: list[tuple[str, int]] = []   # body <200 chars
    per_page: list[int] = []
    offline: list[str] = []      # redirected to home / already offline, not loaded (plan section 2)

    for rec in recs:
        if rec.get("redirected_home"):
            offline.append(rec["url"])
            continue
        try:
            chunks, body_chars = parse_page(rec, args.max_tok, args.overlap_tok, args.doc_type)
        except Exception as e:
            failed.append(f"{rec['url']} :: {type(e).__name__}: {e}")
            continue
        if body_chars == 0:
            failed.append(f"{rec['url']} :: empty extraction")
            continue
        body_lens.append(body_chars)
        per_page.append(len(chunks))
        if body_chars < SHORT_BODY_CHARS:
            short_pages.append((rec["url"], body_chars))
        all_chunks.extend(chunks)

    with open(args.out, "w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    toks = [c["approx_tokens"] for c in all_chunks]
    n_ok = len(body_lens)
    print(f"\n=== 质量报告(plan 阶段四)===")
    parsed = len(recs) - len(offline)
    print(f"页面:{len(recs)}(下线跳过 {len(offline)}) | "
          f"解析成功 {n_ok}/{parsed} ({n_ok/parsed*100:.0f}%) | 抽取失败/空 {len(failed)}")
    if body_lens:
        print(f"正文字符:均值 {statistics.mean(body_lens):.0f} | "
              f"中位 {statistics.median(body_lens):.0f} | "
              f"最短 {min(body_lens)} | 最长 {max(body_lens)}")
    print(f"chunks:{len(all_chunks)} | 每页均值 "
          f"{statistics.mean(per_page):.1f}" if per_page else "chunks:0")
    if toks:
        in_band = sum(1 for t in toks if 300 <= t <= 800)
        tiny = sum(1 for t in toks if t < MIN_CHUNK_TOKENS)
        print(f"token/chunk:中位 {statistics.median(toks):.0f} | "
              f"min {min(toks)} | max {max(toks)} | "
              f"落 300–800 区间 {in_band}/{len(toks)} ({in_band/len(toks)*100:.0f}%) | "
              f"过碎 <{MIN_CHUNK_TOKENS}tok {tiny}")
    if short_pages:
        print(f"\n[正文 <{SHORT_BODY_CHARS} 字符,疑似 JS 渲染/解析失败 {len(short_pages)} 页]")
        for url, n in short_pages:
            print(f"    {n:>4}c  {url}")
    if failed:
        print(f"\n[抽取失败 {len(failed)} 页]")
        for x in failed[:15]:
            print(f"    {x}")

    if all_chunks:
        step = max(1, len(all_chunks) // max(1, args.sample))
        print(f"\n=== 抽样 {min(args.sample, len(all_chunks))} 个 chunk(人工读切分/面包屑)===")
        for c in all_chunks[::step][:args.sample]:
            preview = re.sub(r"\s+", " ", c["text"])[:240]
            print(f"\n  · [{c['approx_tokens']}tok] {c['breadcrumb'] or '(无面包屑)'}")
            print(f"    {c['url']}")
            print(f"    {preview}")

    print(f"\n写入 {args.out}: {len(all_chunks)} 个 chunk")


if __name__ == "__main__":
    main()
