import { Fragment, type ReactNode } from 'react'
import type { AskResult, Course, ProgramFact } from '../api/ask'
import { cnNum, collapseSlots, levelZh, type Slot } from '../lib/courses'

const DISPLAY_CAP = 40
const PROG_CAP = 24

const MODE_ZH: Record<string, string> = {
  filter: '结构化筛选',
  semantic: '语义检索',
  hybrid: '混合检索',
  program: '专业关系',
  empty: '需更具体',
}

// 把回答里的课程码(CSSE1001)包成 <code>,用拆分而非 dangerouslySetInnerHTML
function highlightCodes(text: string): ReactNode[] {
  return text
    .split(/\b([A-Z]{4}\d{4})\b/)
    .map((part, i) => (/^[A-Z]{4}\d{4}$/.test(part) ? <code key={i}>{part}</code> : part))
}

function MoreNote({ total, cap, unit }: { total: number; cap: number; unit: string }) {
  if (total <= cap) return null
  return (
    <div className="note" style={{ padding: '20px 16px 4px' }}>
      还有 {total - cap} {unit}未显示 —— 缩小条件能得到更精确的结果
    </div>
  )
}

function CourseTags({ c }: { c: Course }) {
  const tags: ReactNode[] = []
  if (c.level)
    tags.push(
      <span className="tag" key="lv">
        {levelZh(c.level)}
      </span>,
    )
  if (c.units != null)
    tags.push(
      <span className="tag" key="u">
        {c.units} 学分
      </span>,
    )
  if (c.semester)
    tags.push(
      <span className="tag" key="s">
        {c.semester}
      </span>,
    )
  if (c.has_exam === true)
    tags.push(
      <span className="tag exam" key="e">
        有考试
      </span>,
    )
  if (c.has_exam === false)
    tags.push(
      <span className="tag" key="ne">
        无考试
      </span>,
    )
  if (c.requirement_type === 'core')
    tags.push(
      <span className="tag req" key="r">
        必修
      </span>,
    )
  if (c.requirement_type === 'elective')
    tags.push(
      <span className="tag elec" key="el">
        选修
      </span>,
    )
  if (!tags.length) return null
  return <div className="ctags">{tags}</div>
}

function CourseCard({ c, i }: { c: Course; i: number }) {
  return (
    <div className="course" style={{ animationDelay: `${Math.min(i * 45, 400)}ms` }}>
      <span className="code">{c.code}</span>
      <div className="cmain">
        <div className="ctitle">
          {c.title || <span style={{ color: '#9aa69e' }}>(本学期无开课信息)</span>}
        </div>
        <CourseTags c={c} />
      </div>
      {c.sim != null && <span className="sim">{(c.sim * 100).toFixed(0)}%</span>}
    </div>
  )
}

function GroupCard({ slot, i }: { slot: Slot; i: number }) {
  const choice = `${cnNum(slot.members.length)}选一`
  const isCore = slot.members[0].requirement_type === 'core'
  return (
    <div className="course grp" style={{ animationDelay: `${Math.min(i * 45, 400)}ms` }}>
      <div className="cmain">
        {slot.members.map((m, j) => (
          <Fragment key={m.code}>
            {j > 0 && <div className="oror">或</div>}
            <div className="gline">
              <span className="gcode">{m.code}</span>
              <span>{m.title || ''}</span>
            </div>
          </Fragment>
        ))}
        <div className="ctags">
          {isCore ? (
            <span className="tag req2">{choice}核心</span>
          ) : (
            <span className="tag elec">选修·{choice}</span>
          )}
        </div>
      </div>
    </div>
  )
}

function ProgramRow({ p, i }: { p: ProgramFact; i: number }) {
  const req =
    p.requirement_type === 'core' ? (
      p.equiv_group ? (
        <span className="tag req2">{cnNum(p.equiv_group.split('|').length)}选一核心</span>
      ) : (
        <span className="tag req">必修</span>
      )
    ) : (
      <span className="tag elec">选修</span>
    )
  const via = p.via_plan ? ` · 经 ${p.plan_subtype || p.via_plan}` : ''
  return (
    <div className="prog" style={{ animationDelay: `${Math.min(i * 40, 400)}ms` }}>
      {req}
      <div className="ptitle">
        {p.title}
        <span className="sub2">
          {p.course_list || ''}
          {via}
        </span>
      </div>
    </div>
  )
}

export default function Results({ res }: { res: AskResult }) {
  const isProgList = res.mode === 'program' && Array.isArray(res.program_facts)
  const hasCourses = !!(res.courses && res.courses.length)
  const answerOnly = res.mode === 'empty' || (res.mode === 'program' && !isProgList && !hasCourses)

  const progFacts = isProgList ? (res.program_facts as ProgramFact[]) : []
  const slots = hasCourses ? collapseSlots(res.courses!) : []
  const hasList = isProgList || hasCourses
  const n = isProgList ? progFacts.length : slots.length

  return (
    <>
      <div className="meta">
        <span className="pill" data-m={res.mode}>
          {MODE_ZH[res.mode ?? ''] || res.mode}
        </span>
        {res.mode !== 'empty' && hasList && <span className="count">{n} 条结果</span>}
      </div>

      {res.answer && (
        <div className="answer">
          <h3>回答</h3>
          <div className="body">{highlightCodes(res.answer)}</div>
        </div>
      )}

      {isProgList ? (
        progFacts.length ? (
          <>
            <div className="grid">
              {progFacts.slice(0, PROG_CAP).map((p, i) => (
                <ProgramRow key={`${p.title}-${i}`} p={p} i={i} />
              ))}
            </div>
            <MoreNote total={progFacts.length} cap={PROG_CAP} unit="个专业" />
          </>
        ) : (
          <div className="note">这门课不在任何已收录专业的课表里。</div>
        )
      ) : answerOnly ? null : slots.length ? (
        <>
          <div className="grid">
            {slots
              .slice(0, DISPLAY_CAP)
              .map((s, i) =>
                s.group ? (
                  <GroupCard key={`g-${i}`} slot={s} i={i} />
                ) : (
                  <CourseCard key={s.members[0].code} c={s.members[0]} i={i} />
                ),
              )}
          </div>
          <MoreNote total={slots.length} cap={DISPLAY_CAP} unit="门" />
        </>
      ) : (
        <div className="note">没有命中课程。换个说法或放宽条件试试。</div>
      )}
    </>
  )
}
