import type { DragEvent, ReactNode } from 'react'
import type { SimLocalState } from '../../lib/sim'
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

const dragCode = (e: DragEvent, code: string) => e.dataTransfer.setData('text/plain', code)

export default function RulesPane(props: RulesPaneProps) {
  const { data, csQuery, csResults, offered, goal, advising, advice } = props
  const { onSetBranch, onSetPlan, onPick, onCsearch, onGoalChange, onAdvise } = props

  const ctitle = (c: string) => data.courses[c]?.title || '(无开课信息)'
  const offTag = (c: string) => {
    const o = offered(c)
    return o ? <span className="coff">{o.join('·')}</span> : null
  }
  const lockTag = (c: string) => {
    const l = data.locks?.[c]
    if (!l) return null
    if (l.state === 'unknown') return <span className="clock unk">先修待核</span>
    return (
      <span className="clock" title={l.reason || ''}>
        需先修
      </span>
    )
  }

  const leftCard = (c: string): ReactNode => {
    const locked = data.locks?.[c]?.state === 'locked'
    return (
      <div
        key={c}
        className={`ccard${locked ? ' locked' : ''}`}
        draggable
        onDragStart={(e) => dragCode(e, c)}
        onClick={() => onPick(c)}
      >
        <span className="ccode">{c}</span>
        <span className="ctitle">{ctitle(c)}</span>
        {offTag(c)}
        {lockTag(c)}
      </div>
    )
  }

  const equivCard = (options: string[], k: number): ReactNode => (
    <div key={`eq-${k}`} className="ccard equiv">
      <div style={{ flex: 1, minWidth: 0 }}>
        {options.map((c, i) => (
          <div key={c}>
            {i > 0 && <div className="or">— 二选一 —</div>}
            <div
              draggable
              onDragStart={(e) => dragCode(e, c)}
              onClick={() => onPick(c)}
              style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'grab', minWidth: 0 }}
            >
              <span className="ccode">{c}</span>
              <span className="ctitle">{ctitle(c)}</span>
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
      className="ccard"
      draggable
      onDragStart={(e) => dragCode(e, x.code)}
      onClick={() => onPick(x.code)}
    >
      <span className="ccode">{x.code}</span>
      <span className="ctitle">{x.title || '(无信息)'}</span>
      {x.offerings?.length ? <span className="coff">{x.offerings.join('·')}</span> : null}
    </div>
  )

  const searchSection = (rule: Rule): ReactNode => {
    const picked = data.selected_by_rule?.[rule.ref] || []
    const ph =
      rule.open_scope === 'program' ? '搜程序课表内的课(码/课名)…' : '搜全校任意课程(码/课名)…'
    return (
      <>
        {picked.length > 0 && <div className="clist">{picked.map(leftCard)}</div>}
        <div className="csearch">
          <input
            placeholder={ph}
            value={csQuery[rule.ref] || ''}
            onChange={(e) => onCsearch(rule.ref, e.target.value)}
            autoComplete="off"
          />
          <div className="clist">{(csResults[rule.ref] || []).map(resCard)}</div>
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
      <div className="unatt" key="unatt">
        ⚠ {ov.unattributed!.length} 门课未计入任何规则:{ov.unattributed!.join('、')}
      </div>,
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
          <div className="brtoggle" key={`br-${key}`}>
            <span className="lbl">二选一路径:</span>
            {g.map((ref) => {
              const r2 = data.rules.find((x) => x.ref === ref)
              return (
                <span
                  className={`brpill${ref === cur ? ' on' : ''}`}
                  key={ref}
                  onClick={() => onSetBranch(ref)}
                >
                  {ref} · {r2?.title || ref}
                </span>
              )
            })}
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
        <div className="clist">
          {slots.map((s, i) => (s.kind === 'equiv' ? equivCard(s.options, i) : leftCard(s.code)))}
        </div>
      )
    } else if (rule.open) {
      body = searchSection(rule)
    } else if (rule.children_refs) {
      body = (
        <div className="ttempty">
          由 {rule.children_refs.join(' + ')} 组成,总量 {mn}–{mx} 学分
        </div>
      )
    } else if (!rule.plan_options) {
      body = (
        <div className="ttempty">
          {hasMax && mx > 0
            ? `可修任意课程,最多 ${mx} 学分,本表不逐一枚举`
            : rule.done
              ? '已满足'
              : '可修任意课程,不逐一枚举'}
        </div>
      )
    }

    nodes.push(
      <div className={`rulesec${rule.child_of ? ' childsec' : ''}`} key={rule.ref}>
        <div className="rulehead">
          <span className="rref">{rule.ref}</span>
          <span className="rtitle">{rule.title}</span>
          <span className="runits">
            {typeTag} {label}
            {tail}
          </span>
        </div>
        <div className={`pbar${rule.over_max ? ' over' : ''}`}>
          <i style={{ width: `${pct}%` }}></i>
        </div>
        {rule.plan_options?.map((po) => (
          <div
            key={po.code}
            className={`major${(rule.chosen_plans || []).includes(po.code) ? ' on' : ''}`}
            onClick={() => onSetPlan(po.code)}
          >
            <span className="radio"></span>
            <span style={{ flex: 1 }}>{po.name}</span>
            <span className="runits">{po.units_min}u</span>
          </div>
        ))}
        {body}
      </div>,
    )
  }

  return (
    <section className="pane">
      <div className="panehead">
        <h2>能修的课</h2>
        <span className="hint">拖到右侧 / 点一下自动放</span>
      </div>
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
                resCard({ code: c.code, title: c.title ?? null, offerings: c.offerings || [] }),
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
      {nodes.length ? nodes : <div className="note">没有可选课程。</div>}
    </section>
  )
}
