"""
answerability.py — deterministic answerability gate for KB answers (student-facing red line 3: refuse over wrong).

Purpose: the KB fallback layer must hold the line so that "fictional entity questions (Mars exchange student / Hogwarts college) do not get a story made up from a generic official page",
while **never wrongly refusing a real student's real question**. The refuse signals are pure code, zero LLM, zero TTFT (rule 12):

  1. Year out of range: a study-year year outside [YEAR_LO, YEAR_HI] (e.g. 2099) appears in the question -> refuse.
  2. English entity absent: an English content word from the question that is fully absent from "recalled top-k chunk text ∪ whole-corpus word set"
     (not found in either the whole corpus or the recalled snippets) -> the question points to something not recorded in the store, refuse.

Why absence is only checked for English entities (measured with data/eval/kb_refuse.jsonl, see docs/
rerank_answerability_findings.md): the KB corpus of 2521 chunks is almost all English, so Chinese words are naturally all absent —
checking absence on Chinese words would wrongly refuse **all** real Chinese questions (password / student card / library), crossing the red line.
Chinese "half-related fictional" cases (Mars / space station, with high bi-encoder sim) have no deterministic signal to split on: answerable() only does
deterministic checks that can 100% guarantee no false refusal, and after passing, P2's llm_answerable() (LLM classifier gate) catches Chinese fictional entities.

P2 gate (llm_answerable): after the deterministic gate passes, run one more LLM classification (only judges answerable/not, does not decide high-risk facts,
rule 12). LLM jitter/parse failure always fails open (pass) — "wrongly refusing a real question" hurts more than "missing a fictional one", red line 3's
"false refusal = 0" comes first; missed refusals are visible in caller logs (rule 19). KB_LLM_GATE=0 turns it off (offline / save calls).

The word list data/kb/kb_vocab.txt is produced by build_kb_vocab when the KB is rebuilt. If missing, **raise** rather than silently treat as empty set
(rule 19: config missing must fail loud, otherwise the absence check is always true and refuses in bulk).

Usage:
    from app.services import answerability
    ok, reason = answerability.answerable(question, chunks)   # ok=False means should refuse
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

from app.core.config import DATA_DIR
from app.services import llm

VOCAB_PATH = DATA_DIR / "kb" / "kb_vocab.txt"

# Study-year range covered (currently 2026; keep room backward for transcript/grades, forward for enrolment planning). Out of range = refuse.
YEAR_LO, YEAR_HI = 2020, 2028
_YEAR_RE = re.compile(r"\b(?:19\d{2}|20\d{2})\b")

# English word: starts with a letter, contains letters and digits (same as build_kb_vocab, so the word list and the query use one rule)
EN_WORD = re.compile(r"[a-z][a-z0-9]+")
# Minimum length for an "entity word" to be absence-checked in the question: shorter ones (uq/gpa/vpn etc.) are not treated as entities, to avoid false refusal
_MIN_ENTITY_LEN = 4

# English stopwords: function words / question words / modal words / high-frequency generic verbs. Entity nouns will not fall in here,
# so it is fine to over-list stopwords without weakening the block on fictional entities (only affects "whether to absence-check a word as an entity").
_STOPWORDS = {
    "the", "this", "that", "these", "those", "there", "here",
    "what", "when", "where", "which", "whom", "whose", "while",
    "with", "without", "about", "into", "onto", "over", "under", "after",
    "before", "between", "from", "your", "yours", "mine", "ours", "their",
    "they", "them", "have", "having", "does", "doing", "done", "did", "will",
    "would", "shall", "should", "could", "must", "might", "can", "may",
    "and", "but", "for", "not", "are", "was", "were", "been", "being",
    "how", "why", "who", "you", "get", "got", "getting", "make", "made",
    "take", "taken", "want", "need", "needs", "use", "used", "using", "find",
    "please", "long", "much", "many", "more", "most", "some", "any", "all",
    "out", "off", "than", "then", "also", "just", "very", "too", "still",
    "work", "working", "way", "ways", "thing", "things", "help", "know",
    "tell", "ask", "give", "show", "look", "going", "good", "best", "right",
}


def _out_of_range_year(question: str) -> int | None:
    """First out-of-range study-year year in the question; return None if none are out of range."""
    for tok in _YEAR_RE.findall(question):
        y = int(tok)
        if not (YEAR_LO <= y <= YEAR_HI):
            return y
    return None


def _entity_tokens(question: str) -> list[str]:
    """English entity words in the question to absence-check (lowercase, letter-initial, long enough, not a stopword, deduped in order)."""
    seen: set[str] = set()
    out: list[str] = []
    for w in EN_WORD.findall(question.lower()):
        if len(w) < _MIN_ENTITY_LEN or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _chunk_words(chunks: list[dict]) -> set[str]:
    """English word set from the recalled top-k chunk text (used to exempt "the entity appears in a recalled snippet")."""
    words: set[str] = set()
    for c in chunks:
        words.update(EN_WORD.findall((c.get("text") or "").lower()))
    return words


def load_vocab(path: Path | str = VOCAB_PATH) -> set[str]:
    """Read the word list produced by build_kb_vocab (each line is "word\\tfrequency"), return the word set.
    If the file is missing or empty, always raise — never silently return an empty set (an empty set makes every entity judged absent, refusing in bulk)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"KB 词表缺失:{p};先跑 `python -m app.pipelines.build_kb_vocab`")
    vocab: set[str] = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        word = line.split("\t", 1)[0].strip()
        if word:
            vocab.add(word)
    if not vocab:
        raise ValueError(f"KB 词表为空:{p};请重建")
    return vocab


_VOCAB: set[str] | None = None


def _vocab() -> set[str]:
    """In-process cached word list (loaded on first access; if missing, the raise propagates up)."""
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = load_vocab()
    return _VOCAB


def answerable(question: str, chunks: list[dict], vocab: set[str] | None = None) -> tuple[bool, str]:
    """Answerability check on KB recall results. Returns (ok, reason): ok=False means should refuse (the caller returns [] to trigger KB_REFUSE).

    vocab defaults to the in-process word list (can be injected for unit tests, to avoid depending on disk/DB).
    """
    bad_year = _out_of_range_year(question)
    if bad_year is not None:
        return False, f"学年年份越界:{bad_year}(收录区间 {YEAR_LO}-{YEAR_HI})"

    voc = _vocab() if vocab is None else vocab
    known = voc | _chunk_words(chunks)
    missing = [t for t in _entity_tokens(question) if t not in known]
    if missing:
        return False, f"英文实体缺席(全语料+召回片段均查无):{'、'.join(missing)}"

    return True, "ok"


# ---------- P2: LLM answerability gate (judged after the deterministic gate passes, to catch Chinese fictional entities) ----------

_LLM_GATE_PROMPT = """你是 UQ 学生问答系统的「可答性判定器」。系统已用向量检索从 UQ 官方知识库取到若干页面,
现在要你判断:这些官方页面是否真的覆盖了学生问题问的【那个具体事物】。

判 false(不可答)的唯一情形:问题问的实体/项目/设施/活动在 UQ **并不存在**,属虚构、玩笑或离谱内容
(例:火星交换生、太空站实习、校内滑雪场、魔法学院、在宿舍养宠物龙)。这类即使检索到名字相近的
通用页面(如通用申请页),也判 false——绝不能拿通用页给学生编一套。

判 true(可答)的情形:问题问的是 UQ 真实存在的事务/服务/政策,且页面相关。包括交换生、海外学习、
缴费、census date、改密码、VPN、图书馆借书、缓考、在读证明、奖学金、学生证、住宿、转学分、退费等——
**这些都是真问题,必须判 true**,哪怕页面只是部分相关。判定与语言无关(中英文一视同仁)。

只输出 JSON:{{"answerable": true 或 false, "reason": "简短中文理由"}}

学生问题:{q}

检索到的官方页面标题(按相关度):
{titles}"""


def llm_gate_enabled() -> bool:
    """P2 LLM gate switch (on by default; KB_LLM_GATE=0 turns it off, for offline / save-calls scenarios)."""
    return os.environ.get("KB_LLM_GATE", "1") != "0"


def llm_answerable(question: str, chunks: list[dict]) -> tuple[bool, str]:
    """P2: LLM judges whether the recalled pages really cover the question (catches Chinese fictional entities the deterministic gate cannot).

    Only does classification (rule 12: the LLM does not decide high-risk facts, only judges answerable/not). If the gate is off or there is no chunk, pass directly.
    Parse failure is treated as pass (better to miss a refusal than wrongly refuse a real question, red line 3's "false refusal = 0" comes first; missed refusals are visible in caller logs).
    The exception from the LLM call itself is not swallowed here — it propagates up for the caller to decide fail-open."""
    if not llm_gate_enabled() or not chunks:
        return True, "gate off / no chunk"
    titles = "\n".join(
        f"{i + 1}. {c.get('page_title') or c.get('title') or '(无标题)'}"
        for i, c in enumerate(chunks))
    raw = llm.call([{"role": "user",
                     "content": _LLM_GATE_PROMPT.format(q=question, titles=titles)}],
                   json_mode=True)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return True, f"LLM 门返回非 JSON,放行:{raw[:60]!r}"
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return True, f"LLM 门返回非 JSON,放行:{raw[:60]!r}"
    ok = bool(obj.get("answerable", True))
    return ok, str(obj.get("reason", "") or "")
