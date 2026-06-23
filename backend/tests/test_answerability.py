"""answerability refusal gate: out-of-range year + absent English entity. Inject the vocab; no DB / no disk dependency."""
import pytest

from app.services import answerability
from app.services.answerability import answerable, load_vocab

# Mock full-corpus word set (lowercase English). Chinese is excluded (the corpus is English anyway); used to verify Chinese questions are not wrongly refused by the absence check.
VOCAB = {"scholarship", "apply", "census", "date", "enrolment", "fee",
         "password", "reset", "library", "team", "join", "exchange", "program"}


def _ch(*texts):
    """Build chunks (only the text field is used)."""
    return [{"text": t} for t in texts]


def test_real_english_question_passes():
    ok, reason = answerable("How do I apply for a scholarship?", _ch("apply for a scholarship"), vocab=VOCAB)
    assert ok, reason


def test_fictional_english_entity_refused():
    # underwater / basket / weaving are in neither the full corpus nor the recalled chunks -> refuse
    ok, reason = answerable("How do I join the UQ underwater basket weaving team?",
                            _ch("clubs and teams you can join"), vocab=VOCAB)
    assert not ok
    assert "underwater" in reason and "weaving" in reason


def test_year_out_of_range_refused():
    ok, reason = answerable("UQ 2099 年的本科学费是多少", _ch("tuition fees information"), vocab=VOCAB)
    assert not ok and "2099" in reason


def test_year_in_range_not_refused_by_year_rule():
    # 2026 is within range; the question has no absent English entity -> pass
    ok, reason = answerable("What is the 2026 census date?", _ch("the census date for 2026"), vocab=VOCAB)
    assert ok, reason


def test_chinese_real_question_not_false_refused():
    # A real Chinese question: no English entity word (UQ is too short to count as one), should not be wrongly refused by the absence check (red line)
    ok, reason = answerable("怎么重置 UQ 密码", _ch("reset your UQ password"), vocab=VOCAB)
    assert ok, reason


def test_chinese_fictional_without_year_passes_gate():
    # Chinese fiction (Mars) has no deterministic signal: the gate passes, relying on the min_sim threshold / P2 backstop — here we verify the gate does not wrongly mark it as refusable
    ok, _ = answerable("怎么申请 UQ 的火星交换生项目", _ch("apply for student exchange program"), vocab=VOCAB)
    assert ok


def test_word_absent_from_vocab_but_present_in_chunk_is_known():
    # Appearing in a recalled chunk counts as "the DB has a record", exempting the absence check (top-k text ∪ full-corpus word set)
    ok, _ = answerable("Where is the refectory?", _ch("the campus refectory opening hours"), vocab=VOCAB)
    assert ok


def test_word_absent_everywhere_refused():
    ok, reason = answerable("Where is the quidditch pitch?", _ch("campus map and locations"), vocab=VOCAB)
    assert not ok and "quidditch" in reason


def test_short_tokens_and_stopwords_not_entities():
    # When only short words (gpa/vpn len3) and stopwords are absent from the vocab, they are not treated as entities -> no refuse (avoid wrongly refusing acronyms/function words)
    ok, _ = answerable("Is the GPA on the VPN?", _ch("results page"), vocab=VOCAB)
    assert ok


def test_no_sim_exemption():
    # The gate only looks at vocab/chunks/year, has no sim: high sim does not exempt the absence check (plan: "remove sim exemption")
    ok, _ = answerable("Tell me about the cryptobotany syllabus", _ch("course syllabus overview"), vocab=VOCAB)
    assert not ok


def _patch_llm(monkeypatch, raw):
    monkeypatch.setattr(answerability.llm, "call", lambda *a, **k: raw)


def test_llm_gate_off_passes(monkeypatch):
    # KB_LLM_GATE=0 -> pass directly without calling the LLM (offline / save calls)
    monkeypatch.setenv("KB_LLM_GATE", "0")
    monkeypatch.setattr(answerability.llm, "call",
                        lambda *a, **k: pytest.fail("门关时不应调用 LLM"))
    ok, _ = answerability.llm_answerable("怎么申请火星交换生", _ch("apply"))
    assert ok


def test_llm_gate_no_chunk_passes(monkeypatch):
    monkeypatch.setattr(answerability.llm, "call",
                        lambda *a, **k: pytest.fail("无 chunk 时不应调用 LLM"))
    ok, _ = answerability.llm_answerable("anything", [])
    assert ok


def test_llm_gate_refuses_fictional(monkeypatch):
    _patch_llm(monkeypatch, '{"answerable": false, "reason": "UQ 无火星交换生"}')
    ok, reason = answerability.llm_answerable("怎么申请 UQ 火星交换生项目",
                                              _ch("Submit your application"))
    assert not ok and "火星" in reason


def test_llm_gate_passes_real_question(monkeypatch):
    _patch_llm(monkeypatch, '{"answerable": true, "reason": "真实交换项目"}')
    ok, _ = answerability.llm_answerable("怎么申请海外交换",
                                         _ch("student exchange program"))
    assert ok


def test_llm_gate_malformed_json_fails_open(monkeypatch):
    # Cannot parse JSON -> pass (prioritize zero false-refusals; missed refusals are visible in the log)
    _patch_llm(monkeypatch, "抱歉我无法判断")
    ok, _ = answerability.llm_answerable("怎么重置密码", _ch("reset password"))
    assert ok


def test_llm_gate_extracts_embedded_json(monkeypatch):
    # When JSON comes with extra text/fences, extract the first {...} before judging
    _patch_llm(monkeypatch, '```json\n{"answerable": false, "reason": "虚构"}\n```')
    ok, _ = answerability.llm_answerable("UQ 滑雪场几点开门", _ch("campus facilities"))
    assert not ok


def test_load_vocab_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_vocab(tmp_path / "nope.txt")


def test_load_vocab_empty_file_raises(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        load_vocab(p)


def test_load_vocab_parses_word_freq_lines(tmp_path):
    p = tmp_path / "vocab.txt"
    p.write_text("census\t231\nscholarship\t166\n\n", encoding="utf-8")
    voc = load_vocab(p)
    assert voc == {"census", "scholarship"}
