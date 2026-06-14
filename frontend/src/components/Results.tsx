import { Fragment, type ReactNode } from 'react'
import { Card, Chip } from '@heroui/react'
import { motion, useReducedMotion } from 'motion/react'
import type { AskResult, Course, CourseDetail, KbChunk, ProgramFact } from '../api/ask'
import { cnNum, collapseSlots, levelZh, type Slot } from '../lib/courses'
import { easeOut, riseDelay, riseIn } from '../lib/motion'

const DISPLAY_CAP = 40
const PROG_CAP = 24

// mode -> 来源库标签:对学生有意义的是「答案来自哪个库」,不是检索算法
const LIB_ZH: Record<string, string> = {
  filter: '课程库',
  semantic: '课程库',
  hybrid: '课程库',
  course_detail: '课程库',
  program: '培养方案',
  kb: '知识库',
  empty: '需更具体',
}

const LIB_COLOR: Record<string, 'default' | 'accent' | 'warning'> = {
  filter: 'accent',
  semantic: 'accent',
  hybrid: 'accent',
  course_detail: 'accent',
  program: 'warning',
  kb: 'default',
  empty: 'default',
}

// 把回答里的课程码(CSSE1001)包成 <code>,用拆分而非 dangerouslySetInnerHTML
function highlightCodes(text: string): ReactNode[] {
  return text
    .split(/\b([A-Z]{4}\d{4})\b/)
    .map((part, i) => (/^[A-Z]{4}\d{4}$/.test(part) ? <code key={i}>{part}</code> : part))
}

function Rise({ i, children }: { i: number; children: ReactNode }) {
  const reduce = useReducedMotion()
  if (reduce) return <>{children}</>
  return (
    <motion.div
      initial="hidden"
      animate="show"
      variants={riseIn}
      transition={{ ...easeOut, delay: riseDelay(i) }}
    >
      {children}
    </motion.div>
  )
}

function MoreNote({ total, cap, unit }: { total: number; cap: number; unit: string }) {
  if (total <= cap) return null
  return (
    <div className="pt-5 pb-1 text-center text-[15px] text-muted">
      还有 {total - cap} {unit}未显示 —— 缩小条件能得到更精确的结果
    </div>
  )
}

function CourseTags({ c }: { c: Course }) {
  const tags: ReactNode[] = []
  if (c.level)
    tags.push(
      <Chip size="sm" variant="soft" key="lv">
        {levelZh(c.level)}
      </Chip>,
    )
  if (c.units != null)
    tags.push(
      <Chip size="sm" variant="soft" key="u">
        {c.units} 学分
      </Chip>,
    )
  if (c.semester)
    tags.push(
      <Chip size="sm" variant="soft" key="s">
        {c.semester}
      </Chip>,
    )
  if (c.has_exam === true)
    tags.push(
      <Chip size="sm" variant="soft" color="warning" key="e">
        有考试
      </Chip>,
    )
  if (c.has_exam === false)
    tags.push(
      <Chip size="sm" variant="soft" key="ne">
        无考试
      </Chip>,
    )
  if (c.requirement_type === 'core')
    tags.push(
      <Chip size="sm" variant="primary" color="warning" key="r">
        必修
      </Chip>,
    )
  if (c.requirement_type === 'elective')
    tags.push(
      <Chip size="sm" variant="soft" key="el">
        选修
      </Chip>,
    )
  if (!tags.length) return null
  return <div className="mt-2 flex flex-wrap gap-1.5">{tags}</div>
}

function CourseCard({ c, i }: { c: Course; i: number }) {
  return (
    <Rise i={i}>
      <Card className="flex-row items-start gap-3.5">
        <Chip color="accent" variant="soft" className="shrink-0 font-mono">
          {c.code}
        </Chip>
        <div className="min-w-0 flex-1">
          <div className="text-[15.5px] leading-snug font-semibold">
            {c.title || <span className="text-muted">(本学期无开课信息)</span>}
          </div>
          <CourseTags c={c} />
        </div>
        {c.sim != null && (
          <span className="shrink-0 pt-0.5 text-xs font-semibold text-accent tabular-nums">
            {(c.sim * 100).toFixed(0)}%
          </span>
        )}
      </Card>
    </Rise>
  )
}

function OrLine() {
  return (
    <div className="my-2 flex items-center gap-2 text-[11.5px] font-semibold tracking-wider text-muted">
      <span className="h-px flex-1 bg-separator" />
      或
      <span className="h-px flex-1 bg-separator" />
    </div>
  )
}

function GroupCard({ slot, i }: { slot: Slot; i: number }) {
  const choice = `${cnNum(slot.members.length)}选一`
  const isCore = slot.members[0].requirement_type === 'core'
  return (
    <Rise i={i}>
      <Card>
        {slot.members.map((m, j) => (
          <Fragment key={m.code}>
            {j > 0 && <OrLine />}
            <div className="flex items-baseline gap-2.5 text-[15px] leading-snug font-semibold">
              <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
                {m.code}
              </Chip>
              <span>{m.title || ''}</span>
            </div>
          </Fragment>
        ))}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {isCore ? (
            <Chip size="sm" variant="soft" color="warning">
              {choice}核心
            </Chip>
          ) : (
            <Chip size="sm" variant="soft">
              选修·{choice}
            </Chip>
          )}
        </div>
      </Card>
    </Rise>
  )
}

function ProgramRow({ p, i }: { p: ProgramFact; i: number }) {
  const req =
    p.requirement_type === 'core' ? (
      p.equiv_group ? (
        <Chip size="sm" variant="soft" color="warning" className="shrink-0">
          {cnNum(p.equiv_group.split('|').length)}选一核心
        </Chip>
      ) : (
        <Chip size="sm" variant="primary" color="warning" className="shrink-0">
          必修
        </Chip>
      )
    ) : (
      <Chip size="sm" variant="soft" className="shrink-0">
        选修
      </Chip>
    )
  const via = p.via_plan ? ` · 经 ${p.plan_subtype || p.via_plan}` : ''
  return (
    <Rise i={i}>
      <Card className="flex-row items-center gap-3">
        {req}
        <div className="min-w-0 flex-1 text-[15px] font-medium">
          {p.title}
          <span className="mt-0.5 block text-[12.5px] font-normal text-muted">
            {p.course_list || ''}
            {via}
          </span>
        </div>
      </Card>
    </Rise>
  )
}

function dedupeSources(chunks: KbChunk[]): KbChunk[] {
  const seen = new Set<string>()
  const out: KbChunk[] = []
  for (const c of chunks) {
    if (!c.url || seen.has(c.url)) continue
    seen.add(c.url)
    out.push(c)
  }
  return out
}

function KbSourceCard({ s, i }: { s: KbChunk; i: number }) {
  return (
    <Rise i={i}>
      <a href={s.url} target="_blank" rel="noopener noreferrer" className="block">
        <Card className="transition-colors hover:bg-default-soft">
          <div className="text-[15px] leading-snug font-semibold">
            {s.page_title || s.breadcrumb || s.url}
          </div>
          <div className="mt-0.5 truncate text-[12.5px] text-muted">{s.url}</div>
        </Card>
      </a>
    </Rise>
  )
}

function CourseDetailCard({ c }: { c: CourseDetail }) {
  const tags: ReactNode[] = []
  if (c.level)
    tags.push(
      <Chip size="sm" variant="soft" key="lv">
        {levelZh(c.level)}
      </Chip>,
    )
  if (c.units != null)
    tags.push(
      <Chip size="sm" variant="soft" key="u">
        {c.units} 学分
      </Chip>,
    )
  if (c.semesters && c.semesters.length > 0)
    tags.push(
      <Chip size="sm" variant="soft" key="s">
        {c.semesters.join(' / ')}
      </Chip>,
    )
  if (c.has_exam === true)
    tags.push(
      <Chip size="sm" variant="soft" color="warning" key="e">
        有考试
      </Chip>,
    )
  if (c.has_exam === false)
    tags.push(
      <Chip size="sm" variant="soft" key="ne">
        无考试
      </Chip>,
    )
  if (c.has_hurdle === true)
    tags.push(
      <Chip size="sm" variant="soft" color="warning" key="h">
        有 hurdle
      </Chip>,
    )
  return (
    <Card>
      <div className="flex items-start gap-3">
        <Chip color="accent" variant="soft" className="shrink-0 font-mono">
          {c.code}
        </Chip>
        <div className="min-w-0 flex-1 text-[15.5px] leading-snug font-semibold">{c.title}</div>
      </div>
      {tags.length > 0 && <div className="mt-2.5 flex flex-wrap gap-1.5">{tags}</div>}
      <div className="mt-3 text-[14px]">
        <span className="text-muted">先修:</span> {c.prerequisite_raw || '无先修要求'}
      </div>
      {c.locations && c.locations.length > 0 && (
        <div className="mt-1 text-[14px]">
          <span className="text-muted">校区:</span> {c.locations.join('、')}
        </div>
      )}
      <a
        href={c.profile_url}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-3 inline-block text-[13.5px] font-medium text-accent hover:underline"
      >
        查看官方课程页 →
      </a>
    </Card>
  )
}

export default function Results({
  res,
  streaming = false,
}: {
  res: AskResult
  streaming?: boolean
}) {
  const isCourseDetail = res.mode === 'course_detail'
  const isKb = res.mode === 'kb'
  const isProgList = res.mode === 'program' && Array.isArray(res.program_facts)
  const hasCourses = !!(res.courses && res.courses.length)
  const answerOnly = res.mode === 'empty' || (res.mode === 'program' && !isProgList && !hasCourses)

  const kbSources = isKb ? dedupeSources(res.chunks ?? []) : []
  const progFacts = isProgList ? (res.program_facts as ProgramFact[]) : []
  const slots = hasCourses ? collapseSlots(res.courses!) : []
  const hasList = isProgList || hasCourses
  const n = isProgList ? progFacts.length : slots.length

  return (
    <>
      <div className="mb-4 flex flex-wrap items-center gap-2.5">
        <Chip size="sm" variant="soft" color={LIB_COLOR[res.mode ?? ''] || 'default'}>
          {LIB_ZH[res.mode ?? ''] || res.mode}
        </Chip>
        {isKb
          ? kbSources.length > 0 && (
              <span className="ml-auto text-[13px] text-muted">{kbSources.length} 个来源</span>
            )
          : res.mode !== 'empty' &&
            hasList && <span className="ml-auto text-[13px] text-muted">{n} 条结果</span>}
      </div>

      {(res.answer || streaming) && (
        <Card className="mb-5" variant="secondary">
          <Card.Header>
            <Card.Title className="text-xs font-bold tracking-[0.08em] text-accent uppercase">
              回答
            </Card.Title>
          </Card.Header>
          <Card.Content>
            <div className="text-[15.5px] leading-[1.72] whitespace-pre-wrap [&_code]:rounded-md [&_code]:bg-accent-soft [&_code]:px-1.5 [&_code]:py-px [&_code]:font-mono [&_code]:text-[0.92em] [&_code]:font-semibold [&_code]:text-accent-soft-foreground">
              {highlightCodes(res.answer ?? '')}
              {streaming && (
                <span
                  className="ml-0.5 inline-block h-[1.05em] w-[2px] -translate-y-px animate-pulse bg-accent align-middle"
                  aria-hidden="true"
                />
              )}
            </div>
          </Card.Content>
        </Card>
      )}

      {isCourseDetail ? (
        res.course ? (
          <CourseDetailCard c={res.course} />
        ) : null
      ) : isKb ? (
        kbSources.length ? (
          <div className="grid gap-3">
            {kbSources.map((s, i) => (
              <KbSourceCard key={s.url} s={s} i={i} />
            ))}
          </div>
        ) : null
      ) : isProgList ? (
        progFacts.length ? (
          <>
            <div className="grid gap-3">
              {progFacts.slice(0, PROG_CAP).map((p, i) => (
                <ProgramRow key={`${p.title}-${i}`} p={p} i={i} />
              ))}
            </div>
            <MoreNote total={progFacts.length} cap={PROG_CAP} unit="个专业" />
          </>
        ) : (
          <div className="py-9 text-center text-[15px] text-muted">
            这门课不在任何已收录专业的课表里。
          </div>
        )
      ) : answerOnly ? null : slots.length ? (
        <>
          <div className="grid gap-3">
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
        <div className="py-9 text-center text-[15px] text-muted">
          没有命中课程。换个说法或放宽条件试试。
        </div>
      )}
    </>
  )
}
