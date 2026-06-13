// 模拟器本地状态 + 学期换算工具。

export interface SimLocalState {
  program_id: string
  chosen_plans: string[]
  branch: string[]
  placement: Record<string, number> // code -> 学期格索引
  start_year: number
  years: number
  units_cap: number
  start_sem: string // 入学学期 'S1' | 'S2'
}

const OTHER: Record<string, string> = { S1: 'S2', S2: 'S1' }

function normSem(startSem: string): string {
  return startSem === 'S2' ? 'S2' : 'S1'
}

// 格 i 是 S1 还是 S2:由入学学期决定起点,之后交替。
export function semKind(startSem: string, i: number): string {
  const s = normSem(startSem)
  return i % 2 === 0 ? s : OTHER[s]
}

// 格 i 的日历年:S1 入学每 2 格进 1 年;S2 入学时下一个 S1 已是次年。
export function semYear(startYear: number, startSem: string, i: number): number {
  const s = normSem(startSem)
  return s === 'S1' ? startYear + Math.floor(i / 2) : startYear + Math.floor((i + 1) / 2)
}

const LS_KEY = 'uq_sim_v3'

export function defaultState(): SimLocalState {
  return {
    program_id: '2559',
    chosen_plans: [],
    branch: [],
    placement: {},
    start_year: 2026,
    years: 3,
    units_cap: 8,
    start_sem: 'S1',
  }
}

export function loadState(): SimLocalState {
  try {
    const s = JSON.parse(localStorage.getItem(LS_KEY) || 'null')
    if (s && typeof s === 'object') return { ...defaultState(), ...s }
  } catch {
    // 损坏的 localStorage 退回默认
  }
  return defaultState()
}

export function saveState(s: SimLocalState): void {
  localStorage.setItem(LS_KEY, JSON.stringify(s))
}

let dragCode: string | null = null

export function setDragCode(code: string | null): void {
  dragCode = code
}

export function getDragCode(): string | null {
  return dragCode
}
