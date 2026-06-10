# S2 2026 抓取进度

> 状态:**只抓了 link 清单,profile 详情未爬**(S2 offering 几乎都 unavailable,等上线后再爬)。

## 当前快照(2026-06-10)

- 来源:`https://programs-courses.uq.edu.au/search.html?searchType=course&keywords=&CourseParameters[semester]=2026:2&year=2026`
- S2 2026 搜索页课程码:**2074 门**(去重排序)
- offering 可用性抽样(15 门 / St Lucia / In Person):仅 **1 门**有可用 profile 链接,其余 offering 仍 unavailable

## 产物(link 清单)

| 文件 | 内容 |
|------|------|
| `s2_course_codes.txt` | 2074 个课程码,每行一个(后续爬取的输入) |
| `s2_course_links.tsv` | `course_code` → `course.html` 链接(显式 link 清单) |

## 重要说明

- **St Lucia / In Person 过滤现在做不了**:该过滤依赖每门课的开课表(offering 表),而 S2 offering 几乎都 unavailable。所以本次 link 清单是 **全 S2 2026 课程码(不分校区/模式)**,校区+模式过滤延到后续爬 offering 时再做。
- 与 S1 不同:S1(`course_ids.txt`,1508 门)抓的是已过滤的 **offering_id**;S2 现在只能抓到 **course_code**。

## 后续重爬命令(等 S2 offering 上线后)

```bash
# 1) 重新抓 S2 的 offering_id(此时按 St Lucia / In Person 过滤)
python collect_ids.py --semester 2026:2 --location "St Lucia" --mode "In Person" --out course_ids_s2.txt

# 2) 抓 profile 详情(控制速率)
python scraper.py --file course_ids_s2.txt --out courses_s2.jsonl --delay 1

# 3) 入库(upsert,追加到现有 courses 表)+ embedding
python build_db.py --in courses_s2.jsonl
python embed.py
```
