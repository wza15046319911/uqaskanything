import type { DragEvent } from 'react'
import { type SimLocalState, semKind, semYear } from '../../lib/sim'
import type { SimStateResponse } from '../../api/sim'

interface TimetableProps {
  state: SimLocalState
  data: SimStateResponse
  offered: (code: string) => string[] | null
  onDropCode: (code: string, cell: number) => void
  onRemove: (code: string) => void
  onParam: (patch: Partial<SimLocalState>) => void
  onAuto: () => void
  onClear: () => void
}

export default function Timetable({
  state,
  data,
  onDropCode,
  onRemove,
  onParam,
  onAuto,
  onClear,
}: TimetableProps) {
  const n = state.years * 2
  const val = data.validation
  const capOver = new Set(val.cap_over || [])
  const ctitle = (c: string) => data.courses[c]?.title || '(无开课信息)'

  const placedBy: Record<number, string[]> = {}
  for (const [c, i] of Object.entries(state.placement)) {
    ;(placedBy[i] = placedBy[i] || []).push(c)
  }

  const onDrop = (e: DragEvent, cell: number) => {
    e.preventDefault()
    const code = e.dataTransfer.getData('text/plain')
    if (code) onDropCode(code, cell)
  }

  const ov = data.overall || {}
  const totalU = ov.total_counted ?? data.rules.reduce((a, r) => a + (r.units_counted || 0), 0)
  const nbad = Object.keys(val.by_course || {}).length
  const placedN = Object.keys(state.placement).length
  const unatt = ov.unattributed?.length ?? 0

  return (
    <section className="pane">
      <div className="panehead">
        <h2>时间表</h2>
        <span className="overall">
          已排 {placedN} 门 · {totalU}/{data.total_units}学分
          {ov.formula_satisfied && <span className="ok-txt"> · 学位要求满足✓</span>}
          {data.level_caps
            ?.filter((c) => c.over)
            .map((c) => (
              <span className="bad-txt" key={`${c.scope}-${c.level}`} title={c.text}>
                {' '}
                · L{c.level} {c.used}/{c.max_units}⚠
              </span>
            ))}
          {unatt > 0 && <span className="warn-txt"> · {unatt} 门未计入</span>}
          {nbad > 0 && <span className="bad-txt"> · {nbad} 处冲突</span>}
        </span>
      </div>

      <div className="ttbar">
        <label>
          入学学期{' '}
          <select value={state.start_sem} onChange={(e) => onParam({ start_sem: e.target.value })}>
            <option value="S1">S1 入学</option>
            <option value="S2">S2 入学</option>
          </select>
        </label>
        <label>
          起始年{' '}
          <input
            type="number"
            value={state.start_year}
            min={2020}
            max={2035}
            onChange={(e) => onParam({ start_year: +e.target.value || 2026 })}
          />
        </label>
        <label>
          年数{' '}
          <input
            type="number"
            value={state.years}
            min={1}
            max={6}
            onChange={(e) => onParam({ years: Math.max(1, Math.min(6, +e.target.value || 3)) })}
          />
        </label>
        <label>
          每学期上限{' '}
          <input
            type="number"
            value={state.units_cap}
            min={2}
            max={16}
            step={2}
            onChange={(e) => onParam({ units_cap: +e.target.value || 8 })}
          />
          学分
        </label>
        <button className="btn" onClick={onAuto}>
          一键自动排
        </button>
        <button className="btn warn" onClick={onClear}>
          清空
        </button>
      </div>

      <div className="ttgrid">
        {Array.from({ length: n }, (_, i) => {
          const kind = semKind(state.start_sem, i)
          const year = semYear(state.start_year, state.start_sem, i)
          const u = val.semester_units?.[i] || 0
          const over = capOver.has(i)
          const codes = placedBy[i] || []
          return (
            <div
              key={i}
              className={`ttcell${over ? ' capover' : ''}`}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => onDrop(e, i)}
            >
              <div className="ttcellhead">
                <span className="ttlabel">
                  {kind} {year}
                </span>
                <span className={`ttu${over ? ' bad' : ''}`}>
                  {u}/{val.cap}u
                </span>
              </div>
              {codes.length === 0 && <div className="ttempty">拖课到这里</div>}
              {codes.map((c) => {
                const bad = val.by_course?.[c]
                return (
                  <div key={c}>
                    <div
                      className={`chip${bad ? ' bad' : ''}`}
                      draggable
                      onDragStart={(e) => e.dataTransfer.setData('text/plain', c)}
                    >
                      <span className="ccode">{c}</span>
                      <span className="ct">{ctitle(c)}</span>
                      <span className="x" onClick={() => onRemove(c)}>
                        ×
                      </span>
                    </div>
                    {bad && <div className="badwhy">{bad.map((b) => b.msg).join(';')}</div>}
                  </div>
                )
              })}
            </div>
          )
        })}
      </div>
    </section>
  )
}
