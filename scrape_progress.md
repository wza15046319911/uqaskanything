# 抓取进度 (scrape progress)

> `course_ids.txt` 共 **1508** 个 offering id(S1 2026 / St Lucia / In Person)。
> 本文件记录已抓多少、下一批从哪行开始。
> 入库行数才是准的:`SELECT count(*) FROM courses;`

| 项 | 值 |
|----|----|
| 总 offering id | 1508 |
| 已抓入库 | **120** |
| 进度 | 120 / 1508 (≈ 8.0%) |
| `course_ids.txt` 下一批起始行 | **121** |
| 最后更新 | 2026-06-09 |

## 批次记录

- batch1 — `course_ids.txt` 行 1–20(20 门,ACCT/ABTS 等)
- batch2 — `course_ids.txt` 行 21–120(100 门,扩到 ABTS→BIOL 共 20 个院系)

## 继续抓(增量,幂等)

```bash
# 例:再抓 100 门(行 121–220)。改这里的行号即可
sed -n '121,220p' course_ids.txt > /tmp/ids_next.txt
python scraper.py --file /tmp/ids_next.txt --out /tmp/next.jsonl --delay 0.5
cat /tmp/next.jsonl >> courses.jsonl     # 追加到全量 jsonl
python build_db.py --in courses.jsonl    # upsert,旧课不重抓
python embed.py                          # 只补新增的 embedding
```

抓完后把上面的「已抓入库 / 进度 / 下一批起始行 / 最后更新」改掉。
