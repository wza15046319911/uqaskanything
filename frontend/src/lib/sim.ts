// Simulator local state + semester conversion helpers.

export interface SimLocalState {
  program_id: string
  chosen_plans: string[]
  branch: string[]
  placement: Record<string, number> // code -> semester cell index
  start_year: number
  n_semesters: number
  units_cap: number
  start_sem: string // starting semester 'S1' | 'S2'
}

const OTHER: Record<string, string> = { S1: 'S2', S2: 'S1' }

function normSem(startSem: string): string {
  return startSem === 'S2' ? 'S2' : 'S1'
}

// Whether cell i is S1 or S2: the starting semester sets the start, then they alternate.
export function semKind(startSem: string, i: number): string {
  const s = normSem(startSem)
  return i % 2 === 0 ? s : OTHER[s]
}

// Calendar year of cell i: for S1 entry every 2 cells adds 1 year; for S2 entry the next S1 is already the following year.
export function semYear(startYear: number, startSem: string, i: number): number {
  const s = normSem(startSem)
  return s === 'S1' ? startYear + Math.floor(i / 2) : startYear + Math.floor((i + 1) / 2)
}

// The number of semesters is derived from the degree total units: full load is 8 units/semester, i.e. total units / 8 (rounded up), and may be odd (e.g. 3 semesters for a 1.5-year degree).
// The last cell of placed courses is the lower bound, to avoid pushing courses out of the grid when the semester cap is lowered (part-time).
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
    // corrupted localStorage falls back to default
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
