"""answerability 拒答门:年份越界 + 英文实体缺席。注入词表,无 DB / 无磁盘依赖。"""
import pytest

from app.services import answerability
from app.services.answerability import answerable, load_vocab

# 模拟全语料词集(英文小写)。中文不入(语料本就英文),用于验证中文问题不被缺席判定误拒。
VOCAB = {"scholarship", "apply", "census", "date", "enrolment", "fee",
         "password", "reset", "library", "team", "join", "exchange", "program"}


def _ch(*texts):
    """构造 chunks(只用到 text 字段)。"""
    return [{"text": t} for t in texts]


def test_real_english_question_passes():
    ok, reason = answerable("How do I apply for a scholarship?", _ch("apply for a scholarship"), vocab=VOCAB)
    assert ok, reason


def test_fictional_english_entity_refused():
    # underwater / basket / weaving 全语料 + 召回片段都没有 -> 拒
    ok, reason = answerable("How do I join the UQ underwater basket weaving team?",
                            _ch("clubs and teams you can join"), vocab=VOCAB)
    assert not ok
    assert "underwater" in reason and "weaving" in reason


def test_year_out_of_range_refused():
    ok, reason = answerable("UQ 2099 年的本科学费是多少", _ch("tuition fees information"), vocab=VOCAB)
    assert not ok and "2099" in reason


def test_year_in_range_not_refused_by_year_rule():
    # 2026 在区间内;问题无缺席英文实体 -> 放行
    ok, reason = answerable("What is the 2026 census date?", _ch("the census date for 2026"), vocab=VOCAB)
    assert ok, reason


def test_chinese_real_question_not_false_refused():
    # 中文真问题:英文实体词为空(UQ 太短不算实体),不应被缺席判定误拒(红线)
    ok, reason = answerable("怎么重置 UQ 密码", _ch("reset your UQ password"), vocab=VOCAB)
    assert ok, reason


def test_chinese_fictional_without_year_passes_gate():
    # 中文虚构(火星)无确定性信号:门放行,靠 min_sim 阈值/ P2 兜——这里验证门不误判它为可拒
    ok, _ = answerable("怎么申请 UQ 的火星交换生项目", _ch("apply for student exchange program"), vocab=VOCAB)
    assert ok


def test_word_absent_from_vocab_but_present_in_chunk_is_known():
    # 召回片段里出现即算「库里有记录」,豁免缺席(top-k 文本 ∪ 全语料词集)
    ok, _ = answerable("Where is the refectory?", _ch("the campus refectory opening hours"), vocab=VOCAB)
    assert ok


def test_word_absent_everywhere_refused():
    ok, reason = answerable("Where is the quidditch pitch?", _ch("campus map and locations"), vocab=VOCAB)
    assert not ok and "quidditch" in reason


def test_short_tokens_and_stopwords_not_entities():
    # 仅短词(gpa/vpn len3)和停用词在词表缺席时,不当实体 -> 不拒(避免缩写/功能词误拒)
    ok, _ = answerable("Is the GPA on the VPN?", _ch("results page"), vocab=VOCAB)
    assert ok


def test_no_sim_exemption():
    # 门只看词表/片段/年份,拿不到 sim:高 sim 不豁免缺席(plan「去掉 sim 豁免」)
    ok, _ = answerable("Tell me about the cryptobotany syllabus", _ch("course syllabus overview"), vocab=VOCAB)
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
