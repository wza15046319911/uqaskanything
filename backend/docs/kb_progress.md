# 知识库进度 (KB progress)

> FAQ / 资料页知识库(plan.md)。与 programs/courses 结构化数据互补,做向量路问答。
> 红线约束见 `.claude/rules/student-facing.md`。

| 项 | 值 |
|----|----|
| 阶段一 urls.csv | **1776** 个 URL(support 846 / my.uq 587 / study 343) |
| article 全量 | 抓 **930/930**(0 失败)→ 解析 **928 页** → **1645 chunk** |
| support faq | **498/846 (59%)** = selenium headed 444 + Wayback 部分;余被 Akamai 限速,待续抓 |
| chunks 合计 | **2153**(article 1623 + faq 530);召回 hit@1 82% / hit@3 98%(45 题 golden) |
| 入库 | **已灌 pgvector `kb_chunks` 2153 行**(bge-m3 1024 维 + hnsw 余弦索引) |
| 最后更新 | 2026-06-13 |

## 阶段一:URL 发现(M1 完成)

`python -m app.scrapers.kb_discover` → `data/kb/urls.csv`。

- **support.my.uq.edu.au** 846(FAQ;robots 整站 Disallow,经授权覆盖,见 `ROBOTS_OVERRIDE`)
- **my.uq.edu.au** 587(学生服务)
- **study.uq.edu.au** 343(排除 6967:program/course 详情页 DB 已有 + 营销文)
- 推迟到阶段四 Playwright:`policies.uq.edu.au`(原 ppl 整域 301 迁来,JS SPA 无 sitemap)、
  `library.uq.edu.au`(JS SPA);`graduate-school.uq.edu.au` 域名已下线

## M1.5 先导试点:article 端到端(已验收)

40 篇 article(round-robin 跨 40 个 path_pattern),只走 3c trafilatura 兜底解析器。

| 验收点 | 结果 |
|----|----|
| 解析成功率 | 100% (40/40),无 <200 字符的 JS 失败页 |
| 切分 token/chunk 中位 | 365(调贪心打包前 92);落 300–800 区间 57%;过碎 31→1 |
| chunk 数 | 60(每页 1.5) |
| 召回 hit@1 / hit@3 | **90% / 100%**(10 个真实问题,bge-m3 内存余弦) |

**结论:article 这条路技术可行,效果达标,可放大。**

## article 全量(已抓+解析)

`kb_fetch --type article --per-pattern 0 --max 0` → 930/930 成功(2 个重定向首页标记下线)。
`kb_parse` 全量解析:928 页(跳过 2 下线)解析成功率 100%,产 1645 chunk;
token 中位 520、落 300–800 区间 71%;4 页正文 <200 字符(疑似 JS,归 Playwright 抽查)。

工具:`kb_fetch`(抓 raw)→ `kb_parse`(解析+切分+质量报告)→ `kb_eval`(召回评测)。

数据:`data/kb/{urls.csv, fetched_article.jsonl, raw/, chunks.jsonl, chunk_vecs.jsonl}`。

## 已知粗糙点

- **support 反爬:headed 真实 Chrome 可过,但会被限速**。requests / Playwright-headless /
  后端域 / REST 全 403(Akamai JS sensor)。但 **undetected-chromedriver + headed 真实
  Chrome(非 chromium)能过**(`kb_fetch_selenium.py`,实测稳定)。限速时返回固定 ~509 字符的
  Access Denied 页(`blocked/empty body=509c`),脚本连续失败达 `--stop-after`(默认 12)即早停。
  - **续抓节奏**(2026-06-13):delay 已改成 `--delay-min/--delay-max` 间随机(默认 3–6s,
    篇间 0.5–1.5s 随机,重试更长),避免固定间隔被识别。一批约抓 50–60 篇后仍会撞 Akamai 限速早停;
    **等冷却(经验 ≥20–30min)后 `--resume` 再续一批**,可加大 `--delay-min 6 --delay-max 12`。
    累计 444(selenium)+ Wayback ≈ **498/846**;余 ~348 篇按此分批续。headless 必失败,勿改 headless=True。
  - **Python 3.13 坑**:`undetected-chromedriver==3.5.5`(已是最新)import 了 3.13 移除的
    `distutils` → 必须装 `setuptools<81`(提供 distutils 垫片)才能跑。Chrome 149 + uc 3.5.5 实测兼容。
- 表格页(如 software-content):trafilatura markdown 表格被硬切切碎,正文语义弱,召回偏低。
  阶段三可针对处理或 `include_tables=False`。
- fetch 仍是同步 requests、无 SQLite pages 表;全量持续抓时按 plan 上 httpx+asyncio + SQLite。
- 召回验证是内存余弦;正式入库/检索走 pgvector + `app/services/retrieval.py`(阶段五)。

## 下一步

1. ✅ **阶段五 已建表灌库**:`kb_chunks`(pgvector,带 `source_type`,bge-m3 1024 维 + hnsw)
   已灌 2153 行(article 1623 + faq 530)。`kb_eval` 增量 embed 缓存在 `chunk_vecs.jsonl`,
   `kb_build` join 入库幂等可重跑。**待接检索路由**(`app/services/retrieval.py` + ask 路由),
   让前端能真正问出这 2153 个 chunk。
2. **提升 hit@1**(现 82%):加 rerank(`kb_eval --rerank` cross-encoder 重排 top-30);修
   software-content 表格被切碎(`include_tables=False` 或专门表格处理)。
3. **support 续抓剩余 ~333**(498→513 后撞限速):等 Akamai 冷却,selenium `--resume` + 大 delay
   + 分小批;新增页重跑 `kb_parse faq → 合并 chunks_all → kb_eval 增量 embed → kb_build`。
4. **policies + library**(JS SPA):Playwright 真实浏览器抓,含 article 4 页 <200c JS 抽查。
5. 增量更新(阶段六):sitemap diff + content_hash 比对,学期初加密。
