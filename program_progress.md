# Program 抓取进度 (program scrape progress)

> `program_scraper.py` 抓 program 的【完整规则树】+ 递归展开 plan 分支(major/minor/specialisation/field of study/子program)。
> 输出 `programs.jsonl` → `build_programs.py` 入库 `programs` + `program_course`。

| 项 | 值 |
|----|----|
| 范围 | **全 UQ 5 学院**(eait / hss / hmbs / bel / sci,当前有效,不含归档) |
| **已入库(有课表)** | **335** programs / **66682** program_course 行 |
| 无课表(研究型/归档) | 14(PhD/Doctor 等,正常排除) |
| 异常 | 0 |
| plan 展开 | 是(深度 3,全局缓存去重) |
| 最后更新 | 2026-06-10 |

## 已确认 faculty 代码(year=2026, semester=2026:1, 不含 archived)

| 代码 | 学院 | program 数 |
|------|------|-----------|
| eait | Engineering, Architecture & IT | 84 |
| hss  | Humanities, Arts & Social Sciences(注意码是 hss 非 hass) | 91 |
| hmbs | Health, Medicine & Behavioural Sciences | 90 |
| bel  | Business, Economics & Law | 82 |
| sci  | Science | 97 |

5 院去重后 349 个 program,其中 335 有课表已入库。

## 重跑 / 扩展

```bash
# 全 5 学院(共享 plan 缓存,去重)
python program_scraper.py --faculties eait,hss,hmbs,bel,sci --out programs.jsonl --delay 1.0
python build_programs.py --in programs.jsonl

# 含归档历史版本:加 --archived
```

## 已知事项

- 默认只抓当前有效 program;研究型学位(PhD/Doctor)无 coursework 课表,正常排除(非 bug)。
- 不带 `faculty` 的搜索返回 0,故按学院抓后去重合并。
- program_course 引用全部课程码,与 courses(1508 门 S1/St Lucia/In Person)约半数衔接;其余为 S2/其他校区课,数据范围所致。
