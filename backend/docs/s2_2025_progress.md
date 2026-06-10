# S2 2025 抓取进度

> 状态:**已完成**(profile 全爬 + 入库 + embedding)。2025 S2 是过去学期,offering 全部已发布,和 2026 S2(`s2_progress.md`,仍 deferred)是两回事。

## 快照(2026-06-10)

- 来源:`https://programs-courses.uq.edu.au/search.html?searchType=course&keywords=&CourseParameters[semester]=2025:2&year=2025`
- 过滤口径:**St Lucia / In Person**(和 S1 同口径)
- 搜索页课程码:2105 门 → 该口径命中 **1501 个 offering**(626 门在 St Lucia/In Person 无 S2 开课,13 门多开课)
- 全部爬取成功,**0 失败**;全部入库 + embedding,**0 NULL**

## 产物

| 文件 | 内容 |
|------|------|
| `course_ids_s2_2025.txt` | 1501 个 offering id(St Lucia/In Person 过滤后) |
| `course_ids_s2_2025_remaining.txt` | 续爬用的剩余 540 门清单 |
| `courses_s2_2025.jsonl` | 首轮 961 门 profile |
| `courses_s2_2025_part2.jsonl` | 续爬 540 门 profile |
| `courses_s2_2025_all.jsonl` | 合并去重后 **1501 门**(入库用的最终文件) |

## DB 现状

`courses` 表 = **3009 行**:S1 2026 = 1508 + S2 2025 = 1501,全部已 embedded。
注意:S1 数据是 **2026** 年,S2 是 **2025** 年,同表混存,靠 `semester`/`year` 列区分。下游查询若不带 year/semester 过滤会跨年混返。

## 命令记录(可复现)

```bash
python collect_ids.py --semester 2025:2 --location "St Lucia" --mode "In Person" --out course_ids_s2_2025.txt
python scraper.py --file course_ids_s2_2025.txt --out courses_s2_2025.jsonl --delay 1
# 首轮被外部 SIGTERM 杀在 961 门;算出剩余续爬:
python scraper.py --file course_ids_s2_2025_remaining.txt --out courses_s2_2025_part2.jsonl --delay 0.5
# 合并 -> courses_s2_2025_all.jsonl,入库 + embedding:
python build_db.py --in courses_s2_2025_all.jsonl
python embed.py
```

## eval 回归 + 修复(2026-06-10)

入库后跑 `eval.py`:硬断言全绿(路由 19/19、filter 6/6 EXACT、program 3/3、回归断言 15/15、security 守卫全过),**但语义必含 recall 从 ~100% 掉到 67%**。

根因:`retrieval._fused_search` 按 **offering_id** 去重,同一门课的 S1/2026 与 S2/2025 两个 offering 各占 top-k 一槽,唯一课码从 8 腰斩到 ~6-7,把排在边缘的 must 码挤出 k=8 窗口。用 planner 的同一条英文 query 对比"混库"vs"仅 2026"已坐实:网络安全/数据库/CS无考试三条在仅 2026 都命中、混库都丢。

修复:`retrieval.py` 的 `_fused_search` 和 `filter_search` 改为**按 code 去重**(保留融合分最高/首条 offering)。修复后 recall **67% → 89%**,其余指标不变。

剩余 1 条(网络安全→CYBR7001)非缺陷:S2/2025 新增的 CYBR3000/BISM3205/BISM7213 等更相关课把 CYBR7001 从第 8 挤到第 11——语料真实变大所致,未 gaming k/gold。

## 已知边角

- `embed.py` 的 `blob[:8000]` 字符截断对极少数 token 密度高的课仍超 bge-m3 8192 token 上限:本次 3 门(ANAT1005 / ARTT3100 / PSYC4884)在 8000 下 500,降到 7000 字符成功补回。若以后再遇 500,逐步缩短前缀即可。
