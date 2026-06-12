# 知识库进度 (KB progress)

> FAQ / 资料页知识库(plan.md)。与 programs/courses 结构化数据互补,做向量路问答。
> 红线约束见 `.claude/rules/student-facing.md`。

| 项 | 值 |
|----|----|
| 阶段一 urls.csv | **1776** 个 URL(support 846 / my.uq 587 / study 343) |
| article 全量 | 抓 **930/930**(0 失败)→ 解析 **928 页** → **1645 chunk** |
| support faq | **439/846 (52%)** = selenium headed 376 + Wayback 63;余被 Akamai 限速,待续抓 |
| chunks 合计 | **2102**(article 1645 + faq 457),召回 hit@1 67% / hit@3 93% |
| 最后更新 | 2026-06-12 |

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
  Chrome(非 chromium)能过**(`kb_fetch_selenium.py`,实测稳定)——连续抓约 376 篇后触发
  Akamai 速率限制(8 分钟 0 新增),退避停止。Wayback(`kb_fetch_wayback.py --resume`)
  补了 63 篇,合计 439/846。**余 ~400 篇:等冷却后 selenium `--resume` + 大 delay + 分小批
  续抓,或正式渠道。** headless 必失败,勿改 headless=True。
- 表格页(如 software-content):trafilatura markdown 表格被硬切切碎,正文语义弱,召回偏低。
  阶段三可针对处理或 `include_tables=False`。
- fetch 仍是同步 requests、无 SQLite pages 表;全量持续抓时按 plan 上 httpx+asyncio + SQLite。
- 召回验证是内存余弦;正式入库/检索走 pgvector + `app/services/retrieval.py`(阶段五)。

## 下一步

1. **阶段五(建议优先)**:建 `kb_chunks` 表(pgvector,带 `source_type`)+ KB embed +
   接检索路由,让前端能真正问出这 2102 个已验证 chunk(article+faq)。
2. **提升 hit@1**(现 67%):加 rerank(bge-reranker 重排 top-k);修 software-content
   表格被切碎(`include_tables=False` 或专门表格处理)。
3. **support 续抓剩余 ~400**:等 Akamai 冷却,selenium `--resume` + 大 delay + 分小批。
4. **policies + library**(JS SPA):Playwright 真实浏览器抓,含 article 4 页 <200c JS 抽查。
5. 增量更新(阶段六):sitemap diff + content_hash 比对,学期初加密。
