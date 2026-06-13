"""
answerability.py — KB 答案可答性确定性门(student-facing 红线 3:refuse over wrong)。

目的:KB 兜底层守住「虚构实体问题(火星交换生 / 哈利波特学院)不要拿通用官方页编一套」,
同时**绝不误拒真学生的真问题**。判否信号纯代码、零 LLM、零 TTFT(规则 12):

  1. 年份越界:问题里出现 [YEAR_LO, YEAR_HI] 之外的学年年份(如 2099)-> 拒。
  2. 英文实体缺席:问题的英文内容词,在「召回 top-k chunk 文本 ∪ 全语料词集」里有任一
     完全缺席(全语料 + 召回片段都查无此词)-> 该问指向库里没有记录的东西,拒。

只对英文实体做缺席判定的原因(已用 data/eval/kb_refuse.jsonl 实测,见 docs/
rerank_answerability_findings.md):KB 语料 2521 chunk 几乎全英文,中文词天然全部缺席——
对中文词做缺席判定会把**所有**中文真问题(密码 / 学生证 / 图书馆)误拒,踩穿红线。
中文「半相关虚构」(火星 / 太空站,bi-encoder sim 又高)无确定性信号可分,交 min_sim
阈值兜一部分、其余留给 P2 LLM gate;这里只做能 100% 保证不误拒的确定性判定。

词表 data/kb/kb_vocab.txt 由 build_kb_vocab 随 KB 重建产出。缺失则**抛错**,不静默当空集
(规则 19:配置缺失要 fail loud,否则缺席判定恒为真会批量误拒)。

用法:
    from app.services import answerability
    ok, reason = answerability.answerable(question, chunks)   # ok=False 表示应拒答
"""
from __future__ import annotations
import re
from pathlib import Path

from app.core.config import DATA_DIR

VOCAB_PATH = DATA_DIR / "kb" / "kb_vocab.txt"

# 学年年份收录区间(当前 2026;往回留转录/成绩、往后留选课规划的余量)。越界即拒。
YEAR_LO, YEAR_HI = 2020, 2028
_YEAR_RE = re.compile(r"\b(?:19\d{2}|20\d{2})\b")

# 英文词:字母开头,含字母数字(与 build_kb_vocab 一致,保证词表与查询同口径)
EN_WORD = re.compile(r"[a-z][a-z0-9]+")
# 问题里要做缺席判定的「实体词」最短长度:更短的(uq/gpa/vpn 等)不当实体,避免误拒
_MIN_ENTITY_LEN = 4

# 英文停用词:功能词 / 疑问词 / 情态词 / 高频泛用动词。实体名词不会落进这里,
# 故停用词宁可多列也不削弱虚构实体的拦截力(只影响「要不要把某词当实体来查缺席」)。
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
    """问题里第一个越界的学年年份;无越界返回 None。"""
    for tok in _YEAR_RE.findall(question):
        y = int(tok)
        if not (YEAR_LO <= y <= YEAR_HI):
            return y
    return None


def _entity_tokens(question: str) -> list[str]:
    """问题里要做缺席判定的英文实体词(小写、字母开头、长度达标、非停用词、去重保序)。"""
    seen: set[str] = set()
    out: list[str] = []
    for w in EN_WORD.findall(question.lower()):
        if len(w) < _MIN_ENTITY_LEN or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _chunk_words(chunks: list[dict]) -> set[str]:
    """召回 top-k chunk 文本里的英文词集(用于「实体在召回片段里出现」的豁免)。"""
    words: set[str] = set()
    for c in chunks:
        words.update(EN_WORD.findall((c.get("text") or "").lower()))
    return words


def load_vocab(path: Path | str = VOCAB_PATH) -> set[str]:
    """读 build_kb_vocab 产出的词表(每行「词\\t词频」),返回词集。
    文件缺失或为空一律抛错——绝不静默返回空集(空集会让每个实体都判缺席,批量误拒)。"""
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
    """进程内缓存词表(首次访问加载;缺失抛错向上传播)。"""
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = load_vocab()
    return _VOCAB


def answerable(question: str, chunks: list[dict], vocab: set[str] | None = None) -> tuple[bool, str]:
    """KB 召回结果可答性判定。返回 (ok, reason):ok=False 表示应拒答(上层 return [] 触发 KB_REFUSE)。

    vocab 缺省取进程内词表(可注入做单测,免依赖磁盘/DB)。
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
