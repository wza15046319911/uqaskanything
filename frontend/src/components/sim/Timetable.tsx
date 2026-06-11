import { useEffect, useState, type DragEvent } from 'react'
import { Button, Chip, Label, ListBox, NumberField, Select } from '@heroui/react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { type SimLocalState, semKind, semYear, getDragCode, setDragCode } from '../../lib/sim'
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
  offered,
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
  const [dragOver, setDragOver] = useState<{ cell: number; blocked: boolean } | null>(null)
  const reduce = useReducedMotion()
  const chipAnim = reduce
    ? {}
    : {
        initial: { opacity: 0, scale: 0.96 },
        animate: { opacity: 1, scale: 1 },
        exit: { opacity: 0, scale: 0.96 },
        transition: { duration: 0.18, ease: 'easeOut' as const },
      }

  useEffect(() => {
    const clear = () => {
      setDragCode(null)
      setDragOver(null)
    }
    document.addEventListener('dragend', clear)
    return () => document.removeEventListener('dragend', clear)
  }, [])

  const placedBy: Record<number, string[]> = {}
  for (const [c, i] of Object.entries(state.placement)) {
    ;(placedBy[i] = placedBy[i] || []).push(c)
  }

  const onCellDragOver = (e: DragEvent, cell: number) => {
    const code = getDragCode()
    if (!code) return
    const o = offered(code)
    const blocked = !!(o && !o.includes(semKind(state.start_sem, cell)))
    if (blocked) {
      setDragOver({ cell, blocked: true })
    } else {
      e.preventDefault()
      setDragOver({ cell, blocked: false })
    }
  }

  const onCellDragLeave = (e: DragEvent, cell: number) => {
    if (!e.currentTarget.contains(e.relatedTarget as Node)) {
      setDragOver((d) => (d?.cell === cell ? null : d))
    }
  }

  const onDrop = (e: DragEvent, cell: number) => {
    e.preventDefault()
    setDragOver(null)
    const code = getDragCode() || e.dataTransfer.getData('text/plain')
    setDragCode(null)
    if (code) onDropCode(code, cell)
  }

  const cellCls = (i: number, over: boolean): string => {
    let cls =
      'min-h-24 min-w-0 rounded-xl border-[1.5px] border-dashed border-border bg-background/50 p-2.5 transition-colors'
    if (dragOver?.cell === i) {
      cls += dragOver.blocked
        ? ' border-solid border-danger bg-danger-soft'
        : ' border-solid border-accent bg-accent-soft'
    } else if (over) {
      cls += ' border-warning'
    }
    return cls
  }

  const ov = data.overall || {}
  const totalU = ov.total_counted ?? data.rules.reduce((a, r) => a + (r.units_counted || 0), 0)
  const nbad = Object.keys(val.by_course || {}).length
  const placedN = Object.keys(state.placement).length
  const unatt = ov.unattributed?.length ?? 0

  return (
    <section className="min-w-0 rounded-2xl border border-border bg-surface p-4 shadow-surface lg:sticky lg:top-4 lg:max-h-[calc(100dvh-32px)] lg:self-start lg:overflow-auto">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h2 className="m-0 text-[13px] font-bold tracking-wider text-accent uppercase">时间表</h2>
        <span className="ml-auto flex flex-wrap items-center justify-end gap-1.5 text-xs text-muted tabular-nums">
          已排 {placedN} 门 · {totalU}/{data.total_units}学分
          {ov.formula_satisfied && (
            <Chip size="sm" variant="soft" color="success">
              学位要求满足✓
            </Chip>
          )}
          {data.level_caps
            ?.filter((c) => c.over)
            .map((c) => (
              <Chip
                size="sm"
                variant="soft"
                color="danger"
                key={`${c.scope}-${c.level}`}
                title={c.text}
              >
                L{c.level} {c.used}/{c.max_units}⚠
              </Chip>
            ))}
          {unatt > 0 && (
            <Chip size="sm" variant="soft" color="warning">
              {unatt} 门未计入
            </Chip>
          )}
          {nbad > 0 && (
            <Chip size="sm" variant="soft" color="danger">
              {nbad} 处冲突
            </Chip>
          )}
        </span>
      </div>

      <div className="mb-3 flex flex-wrap items-end gap-2.5">
        <Select
          className="w-32"
          selectedKey={state.start_sem}
          onSelectionChange={(k) => k != null && onParam({ start_sem: String(k) })}
        >
          <Label>入学学期</Label>
          <Select.Trigger>
            <Select.Value />
            <Select.Indicator />
          </Select.Trigger>
          <Select.Popover>
            <ListBox>
              <ListBox.Item id="S1" textValue="S1 入学">
                S1 入学
                <ListBox.ItemIndicator />
              </ListBox.Item>
              <ListBox.Item id="S2" textValue="S2 入学">
                S2 入学
                <ListBox.ItemIndicator />
              </ListBox.Item>
            </ListBox>
          </Select.Popover>
        </Select>
        <NumberField
          value={state.start_year}
          minValue={2020}
          maxValue={2035}
          formatOptions={{ useGrouping: false }}
          onChange={(v) => onParam({ start_year: Number.isFinite(v) ? v : 2026 })}
        >
          <Label>起始年</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="w-14 text-center" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
        <NumberField
          value={state.years}
          minValue={1}
          maxValue={6}
          onChange={(v) => onParam({ years: Number.isFinite(v) ? Math.max(1, Math.min(6, v)) : 3 })}
        >
          <Label>年数</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="w-10 text-center" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
        <NumberField
          value={state.units_cap}
          minValue={2}
          maxValue={16}
          step={2}
          onChange={(v) => onParam({ units_cap: Number.isFinite(v) ? v : 8 })}
        >
          <Label>每学期上限(学分)</Label>
          <NumberField.Group>
            <NumberField.DecrementButton />
            <NumberField.Input className="w-10 text-center" />
            <NumberField.IncrementButton />
          </NumberField.Group>
        </NumberField>
        <Button size="sm" variant="secondary" onPress={onAuto}>
          一键自动排
        </Button>
        <Button size="sm" variant="danger-soft" onPress={onClear}>
          清空
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        {Array.from({ length: n }, (_, i) => {
          const kind = semKind(state.start_sem, i)
          const year = semYear(state.start_year, state.start_sem, i)
          const u = val.semester_units?.[i] || 0
          const over = capOver.has(i)
          const codes = placedBy[i] || []
          return (
            <div
              key={i}
              className={cellCls(i, over)}
              onDragOver={(e) => onCellDragOver(e, i)}
              onDragLeave={(e) => onCellDragLeave(e, i)}
              onDrop={(e) => onDrop(e, i)}
            >
              <div className="mb-1.5 flex items-baseline gap-1.5">
                <span className="text-xs font-semibold text-accent">
                  {kind} {year}
                </span>
                <span
                  className={`ml-auto text-[11px] tabular-nums ${over ? 'font-semibold text-warning' : 'text-muted'}`}
                >
                  {u}/{val.cap}
                </span>
              </div>
              {codes.length === 0 && (
                <div className="px-0.5 py-1.5 text-xs text-muted/70 italic">拖课到这里</div>
              )}
              <AnimatePresence initial={false}>
                {codes.map((c) => {
                  const bad = val.by_course?.[c]
                  return (
                    <motion.div key={c} {...chipAnim}>
                      <div
                        className={`mb-1.5 flex cursor-grab items-center gap-1.5 rounded-lg border bg-surface px-2 py-1.5 text-xs active:cursor-grabbing ${
                          bad ? 'border-danger bg-danger-soft' : 'border-border'
                        }`}
                        draggable
                        onDragStart={(e) => {
                          setDragCode(c)
                          e.dataTransfer.setData('text/plain', c)
                        }}
                      >
                        <Chip
                          size="sm"
                          color="accent"
                          variant="soft"
                          className="shrink-0 font-mono"
                        >
                          {c}
                        </Chip>
                        <span className="min-w-0 flex-1 truncate">{ctitle(c)}</span>
                        <button
                          type="button"
                          aria-label="Remove"
                          className="shrink-0 cursor-pointer px-0.5 text-[15px] leading-none text-muted transition-colors hover:text-danger"
                          onClick={() => onRemove(c)}
                        >
                          ×
                        </button>
                      </div>
                      {bad && (
                        <div className="-mt-0.5 mb-1.5 ml-1 text-[11px] text-danger">
                          {bad.map((b) => b.msg).join(';')}
                        </div>
                      )}
                    </motion.div>
                  )
                })}
              </AnimatePresence>
            </div>
          )
        })}
      </div>
    </section>
  )
}
