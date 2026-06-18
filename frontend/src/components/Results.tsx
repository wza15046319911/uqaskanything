import { Fragment, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Card, Chip, Disclosure } from '@heroui/react'
import { motion, useReducedMotion } from 'motion/react'
import { useTranslation } from 'react-i18next'
import type {
  AskResult,
  Course,
  CourseDetail,
  KbChunk,
  ProgramAnswer,
  ProgramFact,
} from '../api/ask'
import { collapseSlots, levelZh, type Slot } from '../lib/courses'
import { easeOut, riseDelay, riseIn } from '../lib/motion'
import AnswerMarkdown from './AnswerMarkdown'

const DISPLAY_CAP = 40
const PROG_CAP = 24

function PlanInSimLink({ programId, programName }: { programId: string; programName: string }) {
  const { t } = useTranslation()
  return (
    <Link
      to={`/sim?program=${encodeURIComponent(programId)}`}
      className="mt-3 inline-flex items-center gap-1.5 rounded-full bg-accent-soft px-3.5 py-1.5 text-[13.5px] font-semibold text-accent-soft-foreground transition hover:opacity-90"
    >
      {t('results.planInSim', { name: programName })}
    </Link>
  )
}

function Sources({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Disclosure className="mt-4">
      <Disclosure.Heading>
        <Disclosure.Trigger className="inline-flex items-center gap-1 text-[13px] font-medium text-muted transition-colors hover:text-foreground">
          {label}
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-3">{children}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
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
  const { t } = useTranslation()
  if (total <= cap) return null
  return (
    <div className="pt-5 pb-1 text-center text-[15px] text-muted">
      {t('results.moreNote', { n: total - cap, unit })}
    </div>
  )
}

function CourseTags({ c }: { c: Course }) {
  const { t } = useTranslation()
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
        {t('common.units', { n: c.units })}
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
        {t('common.hasExam')}
      </Chip>,
    )
  if (c.has_exam === false)
    tags.push(
      <Chip size="sm" variant="soft" key="ne">
        {t('common.noExam')}
      </Chip>,
    )
  if (c.requirement_type === 'core')
    tags.push(
      <Chip size="sm" variant="primary" color="warning" key="r">
        {t('common.core')}
      </Chip>,
    )
  if (c.requirement_type === 'elective')
    tags.push(
      <Chip size="sm" variant="soft" key="el">
        {t('common.elective')}
      </Chip>,
    )
  if (!tags.length) return null
  return <div className="mt-2 flex flex-wrap gap-1.5">{tags}</div>
}

function CourseCard({ c, i }: { c: Course; i: number }) {
  const { t } = useTranslation()
  return (
    <Rise i={i}>
      <Card className="flex-row items-start gap-3.5">
        <Chip color="accent" variant="soft" className="shrink-0 font-mono">
          {c.code}
        </Chip>
        <div className="min-w-0 flex-1">
          <div className="text-[15.5px] leading-snug font-semibold">
            {c.title || <span className="text-muted">{t('results.noOfferingThisSem')}</span>}
          </div>
          <CourseTags c={c} />
          {c.profile_url && (
            <a
              href={c.profile_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-2 inline-block text-[13px] font-medium text-accent hover:underline"
            >
              {t('results.officialPage')}
            </a>
          )}
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
  const { t } = useTranslation()
  return (
    <div className="my-2 flex items-center gap-2 text-[11.5px] font-semibold tracking-wider text-muted">
      <span className="h-px flex-1 bg-separator" />
      {t('common.or')}
      <span className="h-px flex-1 bg-separator" />
    </div>
  )
}

function GroupCard({ slot, i }: { slot: Slot; i: number }) {
  const { t } = useTranslation()
  const n = slot.members.length
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
              {t('results.chooseOneCore', { n })}
            </Chip>
          ) : (
            <Chip size="sm" variant="soft">
              {t('results.chooseOneElective', { n })}
            </Chip>
          )}
        </div>
      </Card>
    </Rise>
  )
}

function ProgramRow({ p, i }: { p: ProgramFact; i: number }) {
  const { t } = useTranslation()
  const req =
    p.requirement_type === 'core' ? (
      p.equiv_group ? (
        <Chip size="sm" variant="soft" color="warning" className="shrink-0">
          {t('results.chooseOneCore', { n: p.equiv_group.split('|').length })}
        </Chip>
      ) : (
        <Chip size="sm" variant="primary" color="warning" className="shrink-0">
          {t('common.core')}
        </Chip>
      )
    ) : (
      <Chip size="sm" variant="soft" className="shrink-0">
        {t('common.elective')}
      </Chip>
    )
  const via = p.via_plan ? t('results.viaPlan', { plan: p.plan_subtype || p.via_plan }) : ''
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
  const { t } = useTranslation()
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
        {t('common.units', { n: c.units })}
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
        {t('common.hasExam')}
      </Chip>,
    )
  if (c.has_exam === false)
    tags.push(
      <Chip size="sm" variant="soft" key="ne">
        {t('common.noExam')}
      </Chip>,
    )
  if (c.has_hurdle === true)
    tags.push(
      <Chip size="sm" variant="soft" color="warning" key="h">
        {t('common.hasHurdle')}
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
        <span className="text-muted">{t('results.prereq')}</span>{' '}
        {c.prerequisite_raw || t('results.noPrereq')}
      </div>
      {c.locations && c.locations.length > 0 && (
        <div className="mt-1 text-[14px]">
          <span className="text-muted">{t('results.campus')}</span>{' '}
          {c.locations.join(t('common.listSep'))}
        </div>
      )}
      <a
        href={c.profile_url}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-3 inline-block text-[13.5px] font-medium text-accent hover:underline"
      >
        {t('results.viewOfficialPage')}
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
  const { t } = useTranslation()
  const isCourseDetail = res.mode === 'course_detail'
  const isKb = res.mode === 'kb'
  const isProgList = res.mode === 'program' && Array.isArray(res.program_facts)
  const hasCourses = !!(res.courses && res.courses.length)
  const answerOnly = res.mode === 'empty' || (res.mode === 'program' && !isProgList && !hasCourses)

  const kbSources = isKb ? dedupeSources(res.chunks ?? []) : []
  const progFacts = isProgList ? (res.program_facts as ProgramFact[]) : []
  const slots = hasCourses ? collapseSlots(res.courses!) : []
  // program_to_courses / permit: program_facts is a single object with program_id -> gives the one-click jump to the simulator
  const progAnswer =
    res.mode === 'program' && res.program_facts && !Array.isArray(res.program_facts)
      ? (res.program_facts as ProgramAnswer)
      : null

  return (
    <>
      {(res.answer || streaming) && (
        <div className="text-[15.5px] leading-[1.72] [&_a]:font-medium [&_a]:text-accent [&_a]:underline [&_code]:rounded-md [&_code]:bg-accent-soft [&_code]:px-1.5 [&_code]:py-px [&_code]:font-mono [&_code]:text-[0.92em] [&_code]:font-semibold [&_code]:text-accent-soft-foreground [&_h1]:mt-3 [&_h1]:mb-1.5 [&_h1]:text-[17px] [&_h1]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1.5 [&_h2]:text-[16px] [&_h2]:font-semibold [&_h3]:mt-3 [&_h3]:mb-1.5 [&_h3]:text-[15.5px] [&_h3]:font-semibold [&_li]:my-0.5 [&_ol]:my-2 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-0 [&_p+p]:mt-3 [&_strong]:font-semibold [&_ul]:my-2 [&_ul]:list-disc [&_ul]:pl-5">
          <AnswerMarkdown text={res.answer ?? ''} />
          {streaming && (
            <span
              className="ml-0.5 inline-block h-[1.05em] w-[2px] -translate-y-px animate-pulse bg-accent align-middle"
              aria-hidden="true"
            />
          )}
        </div>
      )}

      {progAnswer?.program_id && !streaming && (
        <PlanInSimLink programId={progAnswer.program_id} programName={progAnswer.program} />
      )}

      {isCourseDetail ? (
        res.course ? (
          <Sources label={t('results.courseDetail')}>
            <CourseDetailCard c={res.course} />
          </Sources>
        ) : null
      ) : isKb ? (
        kbSources.length ? (
          <Sources label={t('results.sources', { n: kbSources.length })}>
            <div className="grid gap-3">
              {kbSources.map((s, i) => (
                <KbSourceCard key={s.url} s={s} i={i} />
              ))}
            </div>
          </Sources>
        ) : null
      ) : isProgList ? (
        progFacts.length ? (
          <Sources label={t('results.resultsCount', { n: progFacts.length })}>
            <div className="grid gap-3">
              {progFacts.slice(0, PROG_CAP).map((p, i) => (
                <ProgramRow key={`${p.title}-${i}`} p={p} i={i} />
              ))}
            </div>
            <MoreNote total={progFacts.length} cap={PROG_CAP} unit={t('results.unitProgram')} />
          </Sources>
        ) : (
          <div className="py-9 text-center text-[15px] text-muted">
            {t('results.notInAnyProgram')}
          </div>
        )
      ) : answerOnly ? null : slots.length ? (
        <Sources label={t('results.resultsCount', { n: slots.length })}>
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
          <MoreNote total={slots.length} cap={DISPLAY_CAP} unit={t('results.unitCourse')} />
        </Sources>
      ) : (
        <div className="py-9 text-center text-[15px] text-muted">{t('results.noCourses')}</div>
      )}
    </>
  )
}
