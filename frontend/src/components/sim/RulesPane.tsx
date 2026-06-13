import { useMemo, useRef, useState, type DragEvent, type ReactNode } from 'react'
import {
  Alert,
  Chip,
  ComboBox,
  Input,
  ListBox,
  ProgressBar,
  ToggleButton,
  ToggleButtonGroup,
} from '@heroui/react'
import { type SimLocalState, setDragCode } from '../../lib/sim'
import type { AdviseResponse, Rule, SimCourse, SimStateResponse } from '../../api/sim'

interface RulesPaneProps {
  data: SimStateResponse
  state: SimLocalState
  csQuery: Record<string, string>
  csResults: Record<string, SimCourse[]>
  offered: (code: string) => string[] | null
  goal: string
  advising: boolean
  advice: AdviseResponse | null
  onSetBranch: (ref: string) => void
  onSetPlan: (code: string) => void
  onPick: (code: string) => void
  onCsearch: (ref: string, q: string) => void
  onGoalChange: (g: string) => void
  onAdvise: () => void
}

const dragCode = (e: DragEvent, code: string) => {
  setDragCode(code)
  e.dataTransfer.setData('text/plain', code)
}

const CARD_CLS =
  'flex cursor-grab items-center gap-2 rounded-xl border border-border bg-surface px-2.5 py-2 transition hover:-translate-y-px hover:border-accent active:cursor-grabbing'

export default function RulesPane(props: RulesPaneProps) {
  const { data, csQuery, csResults, offered } = props
  const { onSetBranch, onSetPlan, onPick, onCsearch } = props

  const ctitle = (c: string) => data.courses[c]?.title || '(无开课信息)'

  const paneRef = useRef<HTMLElement>(null)
  const [jumpQ, setJumpQ] = useState('')
  const jumpPool = useMemo(() => {
    const s = new Set<string>()
    Object.values(data.available_by_rule || {}).forEach((slots) =>
      slots.forEach((slot) =>
        slot.kind === 'course' ? s.add(slot.code) : slot.options.forEach((o) => s.add(o)),
      ),
    )
    Object.values(data.selected_by_rule || {}).forEach((arr) => arr.forEach((c) => s.add(c)))
    return [...s]
  }, [data])
  const jq = jumpQ.trim().toLowerCase()
  const jumpHits = jq
    ? jumpPool
        .filter((c) => c.toLowerCase().includes(jq) || ctitle(c).toLowerCase().includes(jq))
        .slice(0, 12)
        .map((c) => ({ code: c, title: ctitle(c) }))
    : []
  const jumpTo = (code: string) => {
    setJumpQ('')
    const el = paneRef.current?.querySelector(`[data-code="${code}"]`)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      el.classList.add('flash')
      window.setTimeout(() => el.classList.remove('flash'), 1200)
    }
  }
  const offTag = (c: string) => {
    const o = offered(c)
    return o ? (
      <Chip size="sm" variant="soft" className="shrink-0">
        {o.join('·')}
      </Chip>
    ) : null
  }
  const lockTag = (c: string) => {
    const l = data.locks?.[c]
    if (!l) return null
    if (l.state === 'unknown')
      return (
        <Chip size="sm" variant="soft" className="shrink-0">
          先修待核
        </Chip>
      )
    return (
      <Chip size="sm" variant="soft" color="warning" className="shrink-0" title={l.reason || ''}>
        需先修
      </Chip>
    )
  }

  const leftCard = (c: string): ReactNode => {
    const locked = data.locks?.[c]?.state === 'locked'
    return (
      <div
        key={c}
        data-code={c}
        className={`${CARD_CLS}${locked ? ' opacity-60' : ''}`}
        draggable
        onDragStart={(e) => dragCode(e, c)}
        onClick={() => onPick(c)}
      >
        <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
          {c}
        </Chip>
        <span className="min-w-0 flex-1 truncate text-[13px]">{ctitle(c)}</span>
        {offTag(c)}
        {lockTag(c)}
      </div>
    )
  }

  const equivCard = (options: string[], k: number): ReactNode => (
    <div key={`eq-${k}`} className={`${CARD_CLS} cursor-default border-dashed`}>
      <div className="min-w-0 flex-1">
        {options.map((c, i) => (
          <div key={c}>
            {i > 0 && <div className="px-1 pt-1 text-[10px] font-bold text-muted">— 二选一 —</div>}
            <div
              data-code={c}
              draggable
              onDragStart={(e) => dragCode(e, c)}
              onClick={() => onPick(c)}
              className="flex min-w-0 cursor-grab items-center gap-2 active:cursor-grabbing"
            >
              <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
                {c}
              </Chip>
              <span className="min-w-0 flex-1 truncate text-[13px]">{ctitle(c)}</span>
              {offTag(c)}
              {lockTag(c)}
            </div>
          </div>
        ))}
      </div>
    </div>
  )

  const resCard = (x: SimCourse): ReactNode => (
    <div
      key={x.code}
      data-code={x.code}
      className={CARD_CLS}
      draggable
      onDragStart={(e) => dragCode(e, x.code)}
      onClick={() => onPick(x.code)}
    >
      <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
        {x.code}
      </Chip>
      <span className="min-w-0 flex-1 truncate text-[13px]">{x.title || '(无信息)'}</span>
      {x.offerings?.length ? (
        <Chip size="sm" variant="soft" className="shrink-0">
          {x.offerings.join('·')}
        </Chip>
      ) : null}
    </div>
  )

  const searchSection = (rule: Rule): ReactNode => {
    const picked = data.selected_by_rule?.[rule.ref] || []
    const ph =
      rule.open_scope === 'program' ? '搜程序课表内的课(码/课名)…' : '搜全校任意课程(码/课名)…'
    return (
      <>
        {picked.length > 0 && <div className="flex flex-col gap-1.5">{picked.map(leftCard)}</div>}
        <div className="mt-1">
          <Input
            placeholder={ph}
            value={csQuery[rule.ref] || ''}
            onChange={(e) => onCsearch(rule.ref, e.target.value)}
            autoComplete="off"
            className="mt-1.5 mb-1 w-full"
          />
          <div className="flex flex-col gap-1.5">{(csResults[rule.ref] || []).map(resCard)}</div>
        </div>
      </>
    )
  }

  // 构建规则节点(命令式:分支药丸只在每组第一次出现时插入)
  const ov = data.overall || {}
  const groups = ov.branch_groups || []
  const chosenBr = ov.branch || {}
  const groupOf: Record<string, string[]> = {}
  groups.forEach((g) => g.forEach((r) => (groupOf[r] = g)))
  const toggled = new Set<string>()
  const nodes: ReactNode[] = []

  if ((ov.unattributed || []).length) {
    nodes.push(
      <Alert status="warning" className="my-2.5" key="unatt">
        <Alert.Indicator />
        <Alert.Content>
          <Alert.Description>
            {ov.unattributed!.length} 门课未计入任何规则:{ov.unattributed!.join('、')}
          </Alert.Description>
        </Alert.Content>
      </Alert>,
    )
  }

  for (const rule of data.rules) {
    const g = groupOf[rule.ref]
    if (g) {
      const key = g.join('|')
      if (!toggled.has(key)) {
        toggled.add(key)
        const cur = chosenBr[key]
        nodes.push(
          <div
            className="my-3 flex flex-wrap items-center gap-2 rounded-xl border border-dashed border-border bg-surface px-2.5 py-2 text-xs"
            key={`br-${key}`}
          >
            <span className="shrink-0 text-muted">二选一路径:</span>
            <ToggleButtonGroup
              selectionMode="single"
              disallowEmptySelection
              selectedKeys={new Set(cur ? [cur] : [])}
              onSelectionChange={(keys: 'all' | Set<string | number>) => {
                if (keys === 'all') return
                const k = [...keys][0]
                if (k != null) onSetBranch(String(k))
              }}
            >
              {g.map((ref) => {
                const r2 = data.rules.find((x) => x.ref === ref)
                return (
                  <ToggleButton key={ref} id={ref} className="max-w-full">
                    <span className="truncate">
                      {ref} · {r2?.title || ref}
                    </span>
                  </ToggleButton>
                )
              })}
            </ToggleButtonGroup>
          </div>,
        )
      }
    }
    if (rule.inactive) continue

    const slots = data.available_by_rule?.[rule.ref] || []
    const typeTag = rule.select_type === 'all' ? '必修' : '选修'
    const hasMax = rule.units_max != null
    const cnt = rule.units_counted
    const mn = rule.units_required
    const mx = rule.units_max ?? 0
    let label: string
    let pct: number
    let tail = ''
    if (!hasMax) {
      label = `${cnt}/${mn}`
      pct = mn > 0 ? Math.min((cnt / mn) * 100, 100) : cnt > 0 ? 100 : 0
      if (rule.done) tail = ' ✓'
    } else {
      label = mn > 0 ? `${cnt} · 需 ${mn}–${mx}` : `${cnt} · 可选 0–${mx}`
      pct = mx > 0 ? Math.min((cnt / mx) * 100, 100) : 0
      if (rule.over_max) tail = ' ·超上限'
      else if (mn > 0 && cnt >= mn) tail = ' ·达下限'
    }

    let body: ReactNode
    if (slots.length) {
      body = (
        <div className="flex flex-col gap-1.5">
          {slots.map((s, i) => (s.kind === 'equiv' ? equivCard(s.options, i) : leftCard(s.code)))}
        </div>
      )
    } else if (rule.open) {
      body = searchSection(rule)
    } else if (rule.children_refs) {
      body = (
        <div className="px-0.5 py-1.5 text-xs text-muted italic">
          由 {rule.children_refs.join(' + ')} 组成,总量 {mn}–{mx} 学分
        </div>
      )
    } else if (!rule.plan_options) {
      body = (
        <div className="px-0.5 py-1.5 text-xs text-muted italic">
          {hasMax && mx > 0
            ? `可修任意课程,最多 ${mx} 学分,本表不逐一枚举`
            : rule.done
              ? '已满足'
              : '可修任意课程,不逐一枚举'}
        </div>
      )
    }

    nodes.push(
      <div
        className={`mb-3.5${rule.child_of ? ' ml-3.5 border-l-2 border-border pl-2.5' : ''}`}
        key={rule.ref}
      >
        <div className="mb-1.5 flex items-baseline gap-2">
          <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
            {rule.ref}
          </Chip>
          <span className="text-[13.5px] font-semibold">{rule.title}</span>
          <span className="ml-auto shrink-0 text-xs whitespace-nowrap text-muted tabular-nums">
            {typeTag} {label}
            {tail}
          </span>
        </div>
        <ProgressBar
          aria-label={rule.title}
          value={pct}
          size="sm"
          color={rule.over_max ? 'warning' : 'accent'}
          className="mb-2"
        >
          <ProgressBar.Track>
            <ProgressBar.Fill />
          </ProgressBar.Track>
        </ProgressBar>
        {rule.plan_options && (
          <ToggleButtonGroup
            className="mb-1.5 flex-col items-stretch"
            selectionMode="single"
            selectedKeys={new Set(rule.chosen_plans || [])}
            onSelectionChange={(keys: 'all' | Set<string | number>) => {
              if (keys === 'all') return
              const k = [...keys][0]
              if (k != null) onSetPlan(String(k))
              else if ((rule.chosen_plans || [])[0]) onSetPlan((rule.chosen_plans || [])[0])
            }}
          >
            {rule.plan_options.map((po) => (
              <ToggleButton key={po.code} id={po.code} className="justify-between gap-2">
                <span className="min-w-0 flex-1 truncate text-left">{po.name}</span>
                <span className="shrink-0 text-xs text-muted tabular-nums">{po.units_min}u</span>
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
        )}
        {body}
      </div>,
    )
  }

  return (
    <section
      className="min-w-0 rounded-2xl border border-border bg-surface p-4 shadow-surface"
      ref={paneRef}
    >
      <div className="mb-3 flex items-center gap-2.5">
        <h2 className="m-0 text-[13px] font-bold tracking-wider text-accent uppercase">能修的课</h2>
        <span className="ml-auto text-xs text-muted">拖到右侧 / 点一下自动放</span>
      </div>
      <div className="mb-2.5">
        <ComboBox
          aria-label="跳到课程:输课程码或课名快速定位…"
          inputValue={jumpQ}
          onInputChange={setJumpQ}
          selectedKey={null}
          onSelectionChange={(key: string | number | null) => {
            if (key != null) jumpTo(String(key))
          }}
          items={jumpHits}
          allowsCustomValue
          menuTrigger="input"
        >
          <ComboBox.InputGroup>
            <Input placeholder="跳到课程:输课程码或课名快速定位…" autoComplete="off" />
            <ComboBox.Trigger />
          </ComboBox.InputGroup>
          <ComboBox.Popover>
            <ListBox>
              {(h: { code: string; title: string }) => (
                <ListBox.Item id={h.code} textValue={`${h.code} ${h.title}`}>
                  <span className="shrink-0 font-mono text-xs font-semibold text-accent">
                    {h.code}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-[13px]">{h.title}</span>
                </ListBox.Item>
              )}
            </ListBox>
          </ComboBox.Popover>
        </ComboBox>
      </div>
      {/* AI 建议(暂时隐藏,代码保留以便恢复)
      <div className="csearch advisebar">
        <input
          placeholder="AI 建议:说说目标,如「想做 AI 安全」…"
          value={goal}
          onChange={(e) => onGoalChange(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && onAdvise()}
          autoComplete="off"
        />
        <button className="btn" onClick={onAdvise} disabled={advising}>
          {advising ? '思考中…' : '建议'}
        </button>
      </div>
      {advising && <div className="ttempty">AI 思考中…(本地模型,约 30–60 秒)</div>}
      {advice && (
        <div>
          {advice.advice ? (
            <div className="advicebox">{advice.advice}</div>
          ) : (
            <div className="ttempty">{advice.note || '没有候选'}</div>
          )}
          {advice.candidates?.length ? (
            <div className="clist">
              {advice.candidates.map((c) =>
                resCard({
                  code: c.code,
                  title: c.title ?? null,
                  offerings: advice.offerings?.[c.code] || [],
                }),
              )}
            </div>
          ) : null}
          {advice.unreachable_count ? (
            <div className="ttempty">
              另有 {advice.unreachable_count} 门可选课无本学期数据,已排除
            </div>
          ) : null}
        </div>
      )}
      */}
      {nodes.length ? (
        nodes
      ) : (
        <div className="py-5 text-center text-sm text-muted">没有可选课程。</div>
      )}
    </section>
  )
}
