import { useMemo, useRef, useState, type DragEvent, type ReactNode } from 'react'
import {
  Alert,
  Button,
  Chip,
  ComboBox,
  Disclosure,
  Input,
  ListBox,
  ProgressBar,
  Select,
  Switch,
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
  'flex cursor-grab items-center gap-3 rounded-xl border border-border bg-surface px-3.5 py-2.5 transition hover:-translate-y-px hover:border-accent hover:shadow-surface active:cursor-grabbing'

// Drop the leading word shared by most rules (like the program short name "BInfTech"), keep only the distinguishing part;
// titles that do not share that prefix (like "General Elective Courses") stay as is, and a title is never cleared.
const stripSharedPrefix = (titles: string[]): string[] => {
  if (titles.length < 2) return titles
  let lists = titles.map((t) => t.split(/\s+/))
  for (;;) {
    const counts = new Map<string, number>()
    lists.forEach((w) => {
      if (w.length > 1) counts.set(w[0], (counts.get(w[0]) || 0) + 1)
    })
    let best: string | null = null
    let bestN = 0
    counts.forEach((n, w) => {
      if (n > bestN) {
        bestN = n
        best = w
      }
    })
    if (best === null || bestN * 2 <= titles.length) break
    lists = lists.map((w) => (w.length > 1 && w[0] === best ? w.slice(1) : w))
  }
  return lists.map((w) => w.join(' '))
}

export default function RulesPane(props: RulesPaneProps) {
  const { data, csQuery, csResults, offered } = props
  const { goal, advising, advice } = props
  const { onSetBranch, onSetPlan, onPick, onCsearch, onGoalChange, onAdvise } = props

  const ctitle = (c: string) => data.courses[c]?.title || '(无开课信息)'

  const titleNode = (c: string): ReactNode => {
    const t = data.courses[c]?.title
    return (
      <span className={`min-w-0 flex-1 truncate text-sm${t ? '' : ' text-muted/70 italic'}`}>
        {t || '无开课信息'}
      </span>
    )
  }

  const paneRef = useRef<HTMLDivElement>(null)
  const [jumpQ, setJumpQ] = useState('')
  const [activeTab, setActiveTab] = useState<string | null>(null)
  const [collapsedOv, setCollapsedOv] = useState<Record<string, boolean>>({})
  const isCollapsed = (r: Rule) => collapsedOv[r.ref] ?? (r.child_of ? true : !!r.done)
  const ruleByRef = useMemo(() => {
    const m: Record<string, Rule> = {}
    data.rules.forEach((r) => (m[r.ref] = r))
    return m
  }, [data.rules])
  // Accordion single-open: when one child rule is opened, collapse its sibling rules under the same parent.
  const toggleRule = (rule: Rule, open: boolean) =>
    setCollapsedOv((m) => {
      const next = { ...m, [rule.ref]: !open }
      if (open && rule.child_of) {
        data.rules.forEach((r) => {
          if (r.ref !== rule.ref && r.child_of === rule.child_of) next[r.ref] = true
        })
      }
      return next
    })
  const ancestorCollapsed = (r: Rule): boolean => {
    let p = r.child_of ? ruleByRef[r.child_of] : undefined
    while (p) {
      if (isCollapsed(p)) return true
      p = p.child_of ? ruleByRef[p.child_of] : undefined
    }
    return false
  }
  const rootOf = (r: Rule): string => {
    let cur = r
    while (cur.child_of && ruleByRef[cur.child_of]) cur = ruleByRef[cur.child_of]
    return cur.ref
  }
  const ruleStatus = (r: Rule) =>
    r.done
      ? { icon: '✓', color: 'text-accent' }
      : (r.units_counted ?? 0) > 0
        ? { icon: '◐', color: 'text-accent' }
        : { icon: '○', color: 'text-muted' }
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
  const codeRule = useMemo(() => {
    const m: Record<string, string> = {}
    const add = (ref: string, code: string) => {
      if (!(code in m)) m[code] = ref
    }
    Object.entries(data.available_by_rule || {}).forEach(([ref, slots]) =>
      slots.forEach((slot) =>
        slot.kind === 'course' ? add(ref, slot.code) : slot.options.forEach((o) => add(ref, o)),
      ),
    )
    Object.entries(data.selected_by_rule || {}).forEach(([ref, arr]) =>
      arr.forEach((c) => add(ref, c)),
    )
    return m
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
    const doScroll = () => {
      const el = paneRef.current?.querySelector(`[data-code="${code}"]`)
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' })
        el.classList.add('flash')
        window.setTimeout(() => el.classList.remove('flash'), 1200)
      }
    }
    const ruleRef = codeRule[code]
    if (!ruleRef) {
      doScroll()
      return
    }
    // Expand the whole ancestor chain of the rule holding the target course, and switch to its top-level tab.
    const chain: string[] = []
    let cur: Rule | undefined = ruleByRef[ruleRef]
    while (cur) {
      chain.push(cur.ref)
      cur = cur.child_of ? ruleByRef[cur.child_of] : undefined
    }
    setCollapsedOv((m) => {
      const next = { ...m }
      chain.forEach((r) => (next[r] = false))
      return next
    })
    const root = chain[chain.length - 1]
    if (root && root !== curTab) setActiveTab(root)
    window.setTimeout(doScroll, 60)
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
      return <span className="shrink-0 text-[11px] text-muted">先修待核</span>
    return (
      <span className="shrink-0 text-[11px] text-warning/80" title={l.reason || ''}>
        需先修
      </span>
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
        {titleNode(c)}
        {offTag(c)}
        {lockTag(c)}
      </div>
    )
  }

  const equivCard = (options: string[], k: number): ReactNode => (
    <div
      key={`eq-${k}`}
      className={`${CARD_CLS} cursor-default flex-col items-stretch border-dashed`}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        {options.map((c, i) => (
          <div key={c}>
            {i > 0 && (
              <div className="mb-2 flex items-center gap-2 text-[10px] font-bold tracking-wider text-muted">
                <span className="h-px flex-1 bg-border" />
                二选一
                <span className="h-px flex-1 bg-border" />
              </div>
            )}
            <div
              data-code={c}
              draggable
              onDragStart={(e) => dragCode(e, c)}
              onClick={() => onPick(c)}
              className="flex min-w-0 cursor-grab items-center gap-3 active:cursor-grabbing"
            >
              <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
                {c}
              </Chip>
              {titleNode(c)}
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
      <span className="min-w-0 flex-1 truncate text-sm">{x.title || '(无信息)'}</span>
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
      <div className="flex flex-col gap-2.5">
        {picked.length > 0 && <div className="flex flex-col gap-2">{picked.map(leftCard)}</div>}
        <div className="flex flex-col gap-2">
          <Input
            placeholder={ph}
            value={csQuery[rule.ref] || ''}
            onChange={(e) => onCsearch(rule.ref, e.target.value)}
            autoComplete="off"
            className="w-full"
          />
          <div className="flex flex-col gap-2">{(csResults[rule.ref] || []).map(resCard)}</div>
        </div>
      </div>
    )
  }

  const planPicker = (rule: Rule): ReactNode => {
    const opts = rule.plan_options || []
    const chosen = (rule.chosen_plans || [])[0]
    if (opts.length === 1) {
      const po = opts[0]
      return (
        <div className="mb-3">
          <Switch isSelected={chosen === po.code} onChange={() => onSetPlan(po.code)}>
            <Switch.Control>
              <Switch.Thumb />
            </Switch.Control>
            <Switch.Content className="flex w-full items-center justify-between gap-2">
              <span className="min-w-0 flex-1 truncate">{po.name}</span>
              <span className="shrink-0 text-xs text-muted tabular-nums">{po.units_min}u</span>
            </Switch.Content>
          </Switch>
        </div>
      )
    }
    const none = '__none__'
    return (
      <div className="mb-3">
        <Select
          aria-label={rule.title}
          className="w-full min-w-0"
          selectedKey={chosen || none}
          onSelectionChange={(k) => {
            const key = k == null ? none : String(k)
            if (key === none) {
              if (chosen) onSetPlan(chosen)
            } else if (key !== chosen) {
              onSetPlan(key)
            }
          }}
        >
          <Select.Trigger>
            <Select.Value />
            <Select.Indicator />
          </Select.Trigger>
          <Select.Popover>
            <ListBox>
              <ListBox.Item id={none} textValue="不选(可选)">
                不选(可选)
                <ListBox.ItemIndicator />
              </ListBox.Item>
              {opts.map((po) => {
                const label = `${po.name} · ${po.units_min}u`
                return (
                  <ListBox.Item key={po.code} id={po.code} textValue={label}>
                    {label}
                    <ListBox.ItemIndicator />
                  </ListBox.Item>
                )
              })}
            </ListBox>
          </Select.Popover>
        </Select>
      </div>
    )
  }

  // Build rule nodes (imperative: the branch pill is inserted only the first time each group appears)
  const ov = data.overall || {}
  const groups = ov.branch_groups || []
  const chosenBr = ov.branch || {}
  const groupOf: Record<string, string[]> = {}
  groups.forEach((g) => g.forEach((r) => (groupOf[r] = g)))
  const toggled = new Set<string>()
  const nodesByRoot: Record<string, ReactNode[]> = {}
  const pushNode = (root: string, node: ReactNode) => {
    ;(nodesByRoot[root] ||= []).push(node)
  }

  const unattNode = (ov.unattributed || []).length ? (
    <Alert status="warning" className="mb-4" key="unatt">
      <Alert.Indicator />
      <Alert.Content>
        <Alert.Description>
          {ov.unattributed!.length} 门课未计入任何规则:{ov.unattributed!.join('、')}
        </Alert.Description>
      </Alert.Content>
    </Alert>
  ) : null

  for (const rule of data.rules) {
    if (ancestorCollapsed(rule)) continue
    const root = rootOf(rule)
    const g = groupOf[rule.ref]
    if (g) {
      const key = g.join('|')
      if (!toggled.has(key)) {
        toggled.add(key)
        const cur = chosenBr[key]
        pushNode(
          root,
          <div
            className="my-4 flex min-w-0 flex-col items-start gap-2 rounded-xl border border-dashed border-border bg-surface px-3.5 py-3 text-xs"
            key={`br-${key}`}
          >
            <span className="font-medium tracking-wide text-muted">二选一路径</span>
            <Select
              aria-label="二选一路径"
              className="w-full min-w-0"
              selectedKey={cur || g[0]}
              onSelectionChange={(k) => k != null && onSetBranch(String(k))}
            >
              <Select.Trigger>
                <Select.Value />
                <Select.Indicator />
              </Select.Trigger>
              <Select.Popover>
                <ListBox>
                  {g.map((ref) => {
                    const r2 = data.rules.find((x) => x.ref === ref)
                    const label = `${ref} · ${r2?.title || ref}`
                    return (
                      <ListBox.Item key={ref} id={ref} textValue={label}>
                        {label}
                        <ListBox.ItemIndicator />
                      </ListBox.Item>
                    )
                  })}
                </ListBox>
              </Select.Popover>
            </Select>
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
        <div className="flex flex-col gap-2">
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

    pushNode(
      root,
      <div
        className={rule.child_of ? 'mb-6 ml-4 border-l-2 border-border pl-4' : 'mb-8'}
        key={rule.ref}
        data-rule={rule.ref}
      >
        <Disclosure
          isExpanded={!isCollapsed(rule)}
          onExpandedChange={(open) => toggleRule(rule, open)}
        >
          <Disclosure.Heading>
            <Disclosure.Trigger className="flex w-full items-baseline gap-2.5 text-left">
              <Chip size="sm" color="accent" variant="soft" className="shrink-0 font-mono">
                {rule.ref}
              </Chip>
              <span className="min-w-0 flex-1 text-sm font-semibold">{rule.title}</span>
              <span
                className={`shrink-0 text-xs whitespace-nowrap tabular-nums ${
                  rule.over_max ? 'text-warning' : rule.done ? 'text-accent' : 'text-muted'
                }`}
              >
                {typeTag} {label}
                {tail}
              </span>
              <Disclosure.Indicator />
            </Disclosure.Trigger>
          </Disclosure.Heading>
          <Disclosure.Content>
            <Disclosure.Body className="pt-2.5">
              <ProgressBar
                aria-label={rule.title}
                value={pct}
                size="sm"
                color={rule.over_max ? 'warning' : 'accent'}
                className="mb-3.5"
              >
                <ProgressBar.Track>
                  <ProgressBar.Fill />
                </ProgressBar.Track>
              </ProgressBar>
              {rule.plan_options && planPicker(rule)}
              {body}
            </Disclosure.Body>
          </Disclosure.Content>
        </Disclosure>
      </div>,
    )
  }

  const counted = ov.total_counted ?? 0
  const totUnits = data.total_units || 0
  const ovPct = totUnits > 0 ? Math.min((counted / totUnits) * 100, 100) : 0
  const topRules = data.rules.filter((r) => !r.child_of && !r.inactive)
  const topTitles = stripSharedPrefix(topRules.map((r) => r.title))
  const curTab =
    activeTab && topRules.some((r) => r.ref === activeTab) ? activeTab : topRules[0]?.ref

  const cardCls = 'min-w-0 rounded-2xl border border-border bg-surface p-5 shadow-surface sm:p-6'

  return (
    <div ref={paneRef} className="flex min-w-0 flex-col gap-4">
      {/* Completion */}
      <section className="sticky top-4 z-10 min-w-0 rounded-2xl border border-border bg-surface px-5 py-4 shadow-surface sm:px-6">
        <div className="mb-3 flex items-center gap-2.5">
          <h2 className="m-0 text-[13px] font-bold tracking-wider text-accent uppercase">
            能修的课
          </h2>
          <span className="ml-auto text-xs text-muted">拖到右侧 / 点一下自动放</span>
        </div>
        {topRules.length > 0 && (
          <>
            <div className="mb-2 flex items-center gap-2">
              <span className="text-xs font-semibold tracking-wide text-muted">学位完成度</span>
              <span className="ml-auto font-mono text-sm tabular-nums">
                <span className="font-semibold text-accent">{counted}</span>
                <span className="text-muted"> / {totUnits} 学分</span>
              </span>
            </div>
            <ProgressBar aria-label="学位完成度" value={ovPct} size="sm" color="accent">
              <ProgressBar.Track>
                <ProgressBar.Fill />
              </ProgressBar.Track>
            </ProgressBar>
          </>
        )}
      </section>
      {/* Search box */}
      <section className={cardCls}>
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
                  <span className="min-w-0 flex-1 truncate text-sm">{h.title}</span>
                </ListBox.Item>
              )}
            </ListBox>
          </ComboBox.Popover>
        </ComboBox>
      </section>
      {/* AI course advice: state the goal in natural language -> engine fixes the pool + LLM ranks within the valid candidates */}
      <section className={cardCls}>
        <div className="mb-3 flex items-center gap-2.5">
          <h2 className="m-0 text-[13px] font-bold tracking-wider text-accent uppercase">
            AI 选课建议
          </h2>
          <span className="ml-auto text-xs text-muted">只在你能修的课里推荐</span>
        </div>
        <div className="flex flex-col gap-2.5">
          <Input
            placeholder="说说你的方向或目标,如「我想做 AI 和机器学习」"
            value={goal}
            onChange={(e) => onGoalChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !advising) onAdvise()
            }}
            autoComplete="off"
            className="w-full"
          />
          <Button isDisabled={advising || !goal.trim()} onPress={onAdvise} className="self-start">
            {advising ? '思考中…' : '给我推荐'}
          </Button>
        </div>
        {advice && (
          <div className="mt-3 flex flex-col gap-3">
            {advice.advice && (
              <div className="rounded-xl border border-border bg-default-soft px-3.5 py-3 text-sm leading-relaxed whitespace-pre-wrap">
                {advice.advice}
              </div>
            )}
            {advice.candidates && advice.candidates.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <span className="text-xs font-semibold tracking-wide text-muted">
                  候选(点一下放进计划)
                </span>
                <div className="flex flex-wrap gap-1.5">
                  {advice.candidates.map((c) => (
                    <button
                      key={c.code}
                      type="button"
                      onClick={() => onPick(c.code)}
                      className="rounded-full bg-default-soft px-2.5 py-1 text-xs transition hover:bg-accent-soft"
                    >
                      <span className="font-mono font-semibold text-accent">{c.code}</span>
                      {c.title ? ` ${c.title}` : ''}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {advice.note && <div className="text-xs text-muted italic">{advice.note}</div>}
            {!!advice.unreachable_count && (
              <div className="text-xs text-muted italic">
                另有 {advice.unreachable_count}{' '}
                门可选课暂无开课/检索信息,未纳入推荐,可在官方课表核对。
              </div>
            )}
          </div>
        )}
      </section>
      {/* Rule list */}
      <section className={cardCls}>
        {topRules.length === 0 ? (
          <div className="py-5 text-center text-sm text-muted">没有可选课程。</div>
        ) : (
          <>
            {unattNode}
            {topRules.length > 1 ? (
              <>
                <Select
                  aria-label="顶层规则"
                  className="mb-4 w-full min-w-0"
                  selectedKey={curTab}
                  onSelectionChange={(k) => k != null && setActiveTab(String(k))}
                >
                  <Select.Trigger>
                    <Select.Value />
                    <Select.Indicator />
                  </Select.Trigger>
                  <Select.Popover>
                    <ListBox>
                      {topRules.map((r, i) => {
                        const st = ruleStatus(r)
                        return (
                          <ListBox.Item key={r.ref} id={r.ref} textValue={topTitles[i]}>
                            <span className={`${st.color} shrink-0`}>{st.icon}</span>
                            <span className="min-w-0 flex-1 truncate">{topTitles[i]}</span>
                            <ListBox.ItemIndicator />
                          </ListBox.Item>
                        )
                      })}
                    </ListBox>
                  </Select.Popover>
                </Select>
                {curTab ? nodesByRoot[curTab] || null : null}
              </>
            ) : (
              nodesByRoot[topRules[0].ref] || null
            )}
          </>
        )}
      </section>
    </div>
  )
}
