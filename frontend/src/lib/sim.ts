// 模拟器本地状态 + 学期换算工具。

export interface SimLocalState {
  program_id: string
  chosen_plans: string[]
  branch: string[]
  placement: Record<string, number> // code -> 学期格索引
  start_year: number
  n_semesters: number
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

// 学期数由学位总学分自动判定:满载 8 学分/学期,即 总学分 / 8(向上取整),可为奇数(如 1.5 年制 3 学期)。
// 已排课的最后一格作为下限,避免学期上限调小(兼读)时把课挤出格子外。
export function computeSemesters(totalUnits: number, placement: Record<string, number>): number {
  const base = Math.ceil((totalUnits || 0) / 8)
  const maxCell = Object.values(placement).reduce((m, i) => Math.max(m, i), -1)
  const needed = maxCell + 1
  return Math.max(1, base, needed)
}

const LS_KEY = 'uq_sim_v3'

export function defaultState(): SimLocalState {
  return {
    program_id: '2559',
    chosen_plans: [],
    branch: [],
    placement: {},
    start_year: 2026,
    n_semesters: 6,
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
