# UQ 知识库爬取计划(FAQ / 资料页)

> 目标:把 UQ 官网的 FAQ、学生服务页、政策文档爬取入库,与已有的 programs/courses 结构化数据互补,构成「SQL + 向量检索」双路问答系统。
>
> 原则:sitemap 优先、原始 HTML 落盘、分类型解析、增量更新。
>
> 给真实学生用的红线约束见 `backend/.claude/rules/student-facing.md`,每加一个数据源都照着守。

---

## 0. 范围与目标域名

| 优先级 | 域名 | 内容 | 类型 | 预估量级 |
|---|---|---|---|---|
| P0 | `support.my.uq.edu.au` | IT / 学生支持 Knowledge Base | 标准 Q&A | 数百篇 |
| P0 | `my.uq.edu.au` | 学生服务主站(enrolment、exams、fees、graduation) | 说明页 | 1–2k 页 |
| P1 | `ppl.app.uq.edu.au` | Policies and Procedures Library(正式政策原始出处) | 政策文档 | 数百份 |
| P1 | `study.uq.edu.au` | entry requirements、fees 等说明页(programs/courses 已有,排除) | 说明页 | 数百页 |
| P2 | `library.uq.edu.au` | 图书馆服务与指南 | 说明页 | 视需要 |
| P2 | `graduate-school.uq.edu.au` | HDR 相关(若覆盖研究生) | 说明页 | 视需要 |

**排除路径模式**(所有域名通用):`/news/`、`/events/`、`/contact/`、`/about/`、staff profile、媒体页。

---

## 1. 阶段一:URL 发现(产出 urls.csv)

1. 对每个域名依次尝试:
   - `https://<domain>/robots.txt` → 读取其中声明的 Sitemap 地址
   - `https://<domain>/sitemap.xml`(可能是 sitemap index,需递归展开)
2. 解析 sitemap,得到全量 URL + lastmod(如有)。
3. 按路径模式做白名单/黑名单过滤。
4. 无 sitemap 的子站降级方案:限定域内 BFS,深度 ≤ 4,种子为该站首页和已知栏目页。
5. 产出 `urls.csv`,字段:`url, domain, path_pattern, guessed_type(faq|policy|article), lastmod, source(sitemap|bfs)`。

**验收:人工扫一遍 urls.csv,确认各域名覆盖面和数量级合理,再进入阶段二。**
(这一步是整个计划里性价比最高的检查点——漏站在此发现,成本为零。)

## 1.5 先导试点(端到端小批量,先抓一部分文章试效果)

正式全量抓取前,先用最小样本把 **article 这条路**端到端跑通,验证解析/切分/召回/答案
质量再规模化——避免白爬全站或垃圾进垃圾出。这一步刻意纵穿到阶段五,但只在 ~40 篇上做,
成本低、迭代快。

- **样本**:从 `urls.csv` 的 `guessed_type=article` 里挑 ~40 篇,覆盖多个 `path_pattern`
  (如 my.uq 的 `/information-and-services/`、study 的 `/admissions/`、`/information-resources/`),
  测解析器鲁棒性,不要只挑同一栏目。
- **只走兜底解析器**:用 3c(`trafilatura` 抽正文 + h2/h3 切分 + 面包屑),先不碰 faq/policy。
- **流程**:小批量 fetch(复用阶段二抓取逻辑,只跑这 40 个 URL)→ article 解析+切分 →
  embed 入库 → 拿 ~10 个真实问题问。
- **验收**(对应红线第 6 条):
  1. 人工读切分后的 chunk:切分边界、面包屑、元数据是否正确;
  2. 问答能否命中正确页面、答案是否带 source URL;
  3. 正文 < 200 字符的页面(疑似 JS 渲染/解析失败)单独标出。
- 效果好 → 放大到全量 article + 横向扩 faq/policy(回到阶段二/M2);
  效果差 → 先调 trafilatura 配置 / 切分参数,**不急着抓全站**。

> 注:这是验证 KB 管线技术可行性的 spike;产品主场景仍按红线第 5 条优先做课程/排课/先修。

## 2. 阶段二:抓取(只存原始 HTML,不解析)

技术栈:`httpx` + `asyncio`(不需要 Scrapy)。

- 并发:同域 2–3 并发,请求间隔 0.5–1s,设置正常浏览器 UA。
- 遵守 robots.txt 的 Disallow 规则。
- 存储:原始 HTML 按 `sha1(url)` 命名落盘到 `raw/<domain>/`,同时在 SQLite 记 `pages` 表:
  `url, url_hash, domain, fetched_at, http_status, content_hash, final_url(重定向后), html_path`
- 失败处理:
  - 4xx/5xx 重试 2 次(指数退避),仍失败记入 `failed.log`
  - 301/302 重定向到站点首页 → 标记为「已下线」,不入库
- PDF 链接(ppl 上常见):单独记到 `pdf_urls.csv`,后续用 pdf 解析流程处理,不混入 HTML 流程。

**为什么先存 HTML 不解析:切分/解析策略一定会迭代,落盘后改策略 = 重跑本地解析,而不是重爬全站。**

## 3. 阶段三:解析与切分(分类型,三套解析器)

### 3a. FAQ/KB 解析器(support.my.uq.edu.au)
- 结构高度统一,CSS selector 直接提取标题(=问题)和正文(=答案)。
- 每篇 KB 文章 = 1 个 chunk(过长的按 h2 再切)。
- chunk 元数据:`{question, answer, url, fetched_at, type: "faq"}`。

### 3b. 政策解析器(ppl.app.uq.edu.au)
- 按 PPL 固定的 section 编号结构切分(如 3.10.02 第 4 节)。
- **保留政策编号和 section 号**,回答引用时可精确到条款。
- chunk 元数据:`{policy_id, policy_title, section_no, section_title, url, type: "policy"}`。

### 3c. 通用文章解析器(其余所有页面,兜底)
- 用 `trafilatura` 抽正文,按 h2/h3 切分。
- 每个 chunk 前缀加面包屑:`页面标题 > h2 > h3`,保证脱离上下文也能被正确召回。
- chunk 元数据:`{page_title, breadcrumb, url, type: "article"}`。

切分通用参数:目标 chunk 300–800 token,超长段落硬切并保留 50 token 重叠。

## 4. 阶段四:质量验收(embed 之前做)

1. 按域名统计:页面数、解析成功率、平均正文长度。
2. 列出正文 < 200 字符的页面 → 人工抽查,通常是解析失败或 JS 渲染页。
3. JS 渲染页(预计很少):汇总后用 Playwright 一次性补爬这一小撮。
4. 随机抽 20 个 chunk 人工读,确认面包屑、切分边界、元数据正确。

**通过验收后才进入 embedding,避免垃圾进垃圾出。**

## 5. 阶段五:入库与路由整合

- chunk 写入现有 pgvector 表(与 programs/courses 共库,新增 `source_type` 字段区分)。
- embedding:全量几千页一次性成本可忽略,模型选现用的即可。
- 路由规则更新:
  - 课程代码 / 专业结构类问题 → SQL 路
  - 程序性 / 政策类问题("如何申请 deferred exam"、"census date 后退课") → 向量路
  - 模糊问题 → 双路并查,结果合并喂给 LLM
- 回答必须带 source URL;政策类回答附 PPL 编号 + section。

## 6. 阶段六:增量更新(每周跑)

1. 重新拉 sitemap,diff 出新增/删除的 URL。
2. 对存量 URL 重爬,比对 `content_hash`,只对变化页面重新解析 + 重新 embed。
3. 已下线页面(404 / 重定向首页)→ 软删除对应 chunk。
4. 学期初(2 月 / 7 月)前后改为每周两次,其余时间每周一次即可。

---

## 执行顺序与里程碑

| 里程碑 | 内容 | 预估工作量 |
|---|---|---|
| M1 | urls.csv 产出 + 人工确认 | 半天 |
| M1.5 | 先导试点:~40 篇 article 端到端跑通(抓→trafilatura 解析→入库→问答),人工验收效果 | 半天 |
| M2 | support.my.uq.edu.au 全流程跑通(爬→解析→入库→可问答) | 1 天 |
| M3 | my.uq.edu.au 入库 | 1 天 |
| M4 | ppl 政策入库(含 PDF 处理) | 1–2 天 |
| M5 | 增量更新脚本 + cron | 半天 |

先把 M2 做完整再横向扩展——一个子站端到端跑通后,其余站点只是换解析器。

## 项目结构建议

```
uq-kb/
├── crawl/
│   ├── discover.py      # 阶段一:sitemap → urls.csv
│   ├── fetch.py         # 阶段二:抓取 → raw/ + pages 表
│   └── refresh.py       # 阶段六:增量更新
├── parse/
│   ├── faq.py           # 3a
│   ├── policy.py        # 3b
│   ├── article.py       # 3c(trafilatura 兜底)
│   └── chunk.py         # 通用切分逻辑
├── qa_check.py          # 阶段四验收报告
├── embed.py             # 阶段五入库
├── raw/                 # 原始 HTML(按域名分目录)
└── kb.sqlite            # pages / failed 记录
```

依赖:`httpx, trafilatura, selectolax, lxml`(Playwright 仅在确认有 JS 页时再装)。