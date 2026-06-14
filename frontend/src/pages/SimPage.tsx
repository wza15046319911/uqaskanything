import { useEffect, useRef, useState } from 'react'
import { Alert, toast } from '@heroui/react'
import ProgramSearch from '../components/sim/ProgramSearch'
import RulesPane from '../components/sim/RulesPane'
import Timetable from '../components/sim/Timetable'
import {
  type Program,
  type SimStateResponse,
  type SimCourse,
  type AdviseResponse,
  fetchPrograms,
  postSimState,
  getSimCourses,
  postSimSchedule,
  postSimAdvise,
} from '../api/sim'
import { type SimLocalState, loadState, saveState, defaultState, semKind } from '../lib/sim'

export default function SimPage() {
  const [state, setState] = useState<SimLocalState>(loadState)
  const [programs, setPrograms] = useState<Program[]>([])
  const [data, setData] = useState<SimStateResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [csQuery, setCsQuery] = useState<Record<string, string>>({})
  const [csResults, setCsResults] = useState<Record<string, SimCourse[]>>({})
  const [extraOff, setExtraOff] = useState<Record<string, string[]>>({})
  const [goal, setGoal] = useState('')
  const [advising, setAdvising] = useState(false)
  const [advice, setAdvice] = useState<AdviseResponse | null>(null)
  const csTimer = useRef<number | undefined>(undefined)

  const showToast = (m: string) => {
    toast(m)
  }

  const refresh = async (s: SimLocalState) => {
    try {
      const d = await postSimState({
        program_id: s.program_id,
        selected: Object.keys(s.placement),
        chosen_plans: s.chosen_plans,
        branch: s.branch,
        placement: s.placement,
        units_cap: s.units_cap,
        n_semesters: s.years * 2,
        start_sem: s.start_sem,
      })
      if (d.error) {
        setErr(d.error)
        return
      }
      setErr(null)
      setData(d)
    } catch (e) {
      setErr(`连不上服务:${e instanceof Error ? e.message : String(e)}`)
    }
  }

  useEffect(() => {
    fetchPrograms()
      .then(setPrograms)
      .catch(() => setPrograms([]))
    void refresh(state)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const apply = (next: SimLocalState) => {
    setState(next)
    saveState(next)
    void refresh(next)
  }

  const pickProgram = (id: string) => {
    setCsQuery({})
    setCsResults({})
    setExtraOff({})
    setAdvice(null)
    apply({
      ...defaultState(),
      program_id: id,
      start_year: state.start_year,
      years: state.years,
      units_cap: state.units_cap,
      start_sem: state.start_sem,
    })
  }

  const offered = (code: string): string[] | null =>
    (data?.offerings && data.offerings[code]) || extraOff[code] || null

  const place = (code: string, cell: number) => {
    apply({ ...state, placement: { ...state.placement, [code]: cell } })
  }

  const dropPlace = (code: string, cell: number) => {
    const o = offered(code)
    const kind = semKind(state.start_sem, cell)
    if (o && !o.includes(kind)) {
      showToast(`${code} 不在 ${kind} 开课(开:${o.join('/')})`)
      return
    }
    place(code, cell)
  }

  const unplace = (code: string) => {
    const p = { ...state.placement }
    delete p[code]
    apply({ ...state, placement: p })
  }

  const autoPlace = (code: string) => {
    const o = offered(code)
    const n = state.years * 2
    let target = -1
    for (let i = 0; i < n; i++) {
      if (!o || o.includes(semKind(state.start_sem, i))) {
        target = i
        break
      }
    }
    if (target < 0) target = 0
    place(code, target)
  }

  const setBranch = (ref: string) => apply({ ...state, branch: [ref] })
  const setPlan = (code: string) =>
    apply({ ...state, chosen_plans: state.chosen_plans.includes(code) ? [] : [code] })
  const setParam = (patch: Partial<SimLocalState>) => apply({ ...state, ...patch })
  const doClear = () => apply({ ...state, placement: {} })

  const onCsearch = (ref: string, q: string) => {
    setCsQuery((m) => ({ ...m, [ref]: q }))
    window.clearTimeout(csTimer.current)
    if (q.trim().length < 2) {
      setCsResults((m) => ({ ...m, [ref]: [] }))
      return
    }
    csTimer.current = window.setTimeout(async () => {
      const rule = data?.rules.find((r) => r.ref === ref)
      const rows = await getSimCourses(
        q.trim(),
        rule?.open_scope === 'program' ? state.program_id : undefined,
      )
      const list = Array.isArray(rows) ? rows : []
      setCsResults((m) => ({ ...m, [ref]: list }))
      setExtraOff((m) => {
        const next = { ...m }
        list.forEach((x) => {
          if (x.offerings?.length) next[x.code] = x.offerings
        })
        return next
      })
    }, 250)
  }

  const doAdvise = async () => {
    if (!goal.trim()) {
      showToast('先写一句目标')
      return
    }
    setAdvising(true)
    setAdvice(null)
    try {
      const d = await postSimAdvise({
        program_id: state.program_id,
        selected: Object.keys(state.placement),
        chosen_plans: state.chosen_plans,
        branch: state.branch,
        goal: goal.trim(),
      })
      if (d.error) {
        showToast('建议出错:' + d.error)
        return
      }
      setAdvice(d)
      setExtraOff((m) => ({ ...m, ...(d.offerings || {}) }))
    } catch (e) {
      showToast('连不上服务:' + (e instanceof Error ? e.message : String(e)))
    } finally {
      setAdvising(false)
    }
  }

  const doAuto = async () => {
    const sel = Object.keys(state.placement)
    if (!sel.length) {
      showToast('先把课拖进来或点进来,再自动排')
      return
    }
    const d = await postSimSchedule({
      program_id: state.program_id,
      selected: sel,
      chosen_plans: state.chosen_plans,
      units_cap: state.units_cap,
      start_sem: state.start_sem,
    })
    if (d.error) {
      showToast('排课出错:' + d.error)
      return
    }
    const np: Record<string, number> = {}
    d.semesters.forEach((s, i) => s.courses.forEach((x) => (np[x.code] = i)))
    d.unplaced.forEach((u) => {
      if (!(u.code in np)) np[u.code] = 0
    })
    const years =
      d.semesters.length > state.years * 2 ? Math.ceil(d.semesters.length / 2) : state.years
    apply({ ...state, placement: np, years })
    if (d.unplaced.length) showToast(`${d.unplaced.length} 门排不下(学期/上限不够),已置 Y1 待调`)
  }

  const current = programs.find((p) => p.program_id === state.program_id)

  return (
    <div className="mx-auto max-w-[1180px] px-5 pt-[clamp(20px,4vw,40px)] pb-20 text-[15px]">
      <header className="mb-5 text-center">
        <h1 className="mb-1.5 text-[clamp(26px,5vw,40px)] leading-[1.05] font-semibold tracking-tight">
          UQ <em className="text-accent not-italic">Program</em> Planner
        </h1>
        <ProgramSearch programs={programs} current={current} onPick={pickProgram} />
      </header>

      {err && (
        <Alert status="danger" className="mb-4">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title>出错</Alert.Title>
            <Alert.Description>{err}</Alert.Description>
          </Alert.Content>
        </Alert>
      )}
      {!data && !err && (
        <div className="py-6 text-center text-sm text-muted">先在上方搜一个专业开始。</div>
      )}

      {data && (
        <div className="mt-5 grid items-start gap-4 lg:grid-cols-[minmax(0,0.95fr)_minmax(0,1.25fr)]">
          <RulesPane
            data={data}
            state={state}
            csQuery={csQuery}
            csResults={csResults}
            offered={offered}
            goal={goal}
            advising={advising}
            advice={advice}
            onSetBranch={setBranch}
            onSetPlan={setPlan}
            onPick={autoPlace}
            onCsearch={onCsearch}
            onGoalChange={setGoal}
            onAdvise={doAdvise}
          />
          <Timetable
            state={state}
            data={data}
            offered={offered}
            onDropCode={dropPlace}
            onRemove={unplace}
            onParam={setParam}
            onAuto={doAuto}
            onClear={doClear}
          />
        </div>
      )}
    </div>
  )
}
