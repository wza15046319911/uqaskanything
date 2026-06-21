# 课程攻略语料 — 作者规则

攻略只当「经验层」补充,**绝不是事实源**。事实(先修/考核占比/Hurdle/日期)永远以官方课程大纲
(ECP)为准,系统从 `course_detail` 取。攻略价值在 DB 没有的东西:口试是不是 in-person、踩坑、
guest lecture 爱考、best-N-of-M、准备建议。

## 文件命名
`<COURSE_CODE>_<YEAR>.md`,一课一篇一年。COMP4500 / COMP7500 是两门课(先修、权重都不同),
**必须拆成两个文件**,绝不合写。

## frontmatter(机器对账读这里)
```yaml
---
course_code: INFS7410        # 必填,大写课程码
year: 2025                   # 必填,经验对应的年份
semester: S2                 # 选填
source: 学长经验贴(匿名)    # 必填,答案渲染「据 20XX 经验」用
nature: subjective           # 必填,固定 subjective
claims:                      # 入库前 guide_check 逐项比 course_detail
  prereq: "INFS2200 or INFS7903"
  assessment:
    - {name: Quiz, weight: 10}
    - {name: Project Part 1, weight: 10}
    - {name: Project Part 2, weight: 30}
    - {name: Final Oral Exam, weight: 50, hurdle: true}
checked: ""                  # 对账通过后由 guide_build 回填日期,作者留空
---
```

## 硬规则
1. 事实进 `claims`,正文**不写裸权重 / 裸先修当事实**;要提就带「(以当年 ECP 为准)」。
2. `claims.assessment` 的每项 `weight` 必须和官方占比一致;Hurdle 项必须标 `hurdle: true`
   —— 漏标会被 `guide_check` 当冲突拦下(防 COMP 那类「把期末 Hurdle 写漏」)。
3. 裸日期删掉或显式标年:「11 月 10–21 日」→「2025 年 11 月那两周」。
4. `nature: subjective` + `source` 必填。
5. 写完跑 `python -m app.pipelines.guide_check data/guides/<code>_<year>.md`,通过后 `checked` 才会有值。
   再跑 `python -m app.pipelines.guide_build` 入库。

## 正文(经验层)
按 `##` 分节,每节是一个检索块。只放无法结构化的经验:讲什么、考核形式的体感、避坑、准备建议。
