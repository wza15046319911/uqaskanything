// Course planning simulator API layer. Types match the return shape of backend/app/api/sim.py.

import { turnstileHeaders } from '../lib/turnstile'

export interface Program {
  program_id: string
  title: string
  total_units: number
}

export interface PlanOption {
  code: string
  name: string
  units_min: number
}

export interface Rule {
  ref: string
  title: string
  inactive?: boolean
  select_type?: string // 'all' = required; otherwise elective
  units_counted: number
  units_required: number
  units_max?: number | null
  done?: boolean
  over_max?: boolean
  open?: boolean
  open_scope?: string // 'program' | 'any'
  plan_options?: PlanOption[]
  chosen_plans?: string[]
  child_of?: string
  children_refs?: string[]
}

// Slot of available_by_rule: a single course or a pick-one-of-two group
export type RuleSlot = { kind: 'course'; code: string } | { kind: 'equiv'; options: string[] }

export interface Overall {
  branch?: Record<string, string>
  branch_groups?: string[][]
  total_counted?: number
  formula_satisfied?: boolean
  unattributed?: string[]
}

export interface Lock {
  state: string // 'locked' | 'unknown'
  reason?: string
}

export interface ValidationReason {
  type: string
  msg: string
}

export interface Validation {
  by_course: Record<string, ValidationReason[]>
  semester_units: number[]
  cap_over: number[]
  cap: number
}

export interface LevelCap {
  kind?: string // 'level_max' (upper bound) | 'level_min' (lower bound)
  level: number
  used: number
  max_units?: number // level_max
  min_units?: number // level_min
  over?: boolean // over the upper bound
  under?: boolean // below the lower bound
  satisfied?: boolean
  or_higher?: boolean
  scope?: string // 'program' | 'electives' | 'field' | 'sub:<ref>'
  text?: string
}

export interface CourseMeta {
  code: string
  title: string | null
  units?: number | null
  level?: string | null
  semester?: string | null
  has_exam?: boolean | null
}

export interface SimStateResponse {
  program_id: string
  title: string
  total_units: number
  selected: string[]
  chosen_plans: string[]
  rules: Rule[]
  available_by_rule: Record<string, RuleSlot[]>
  selected_by_rule: Record<string, string[]>
  overall: Overall
  locks: Record<string, Lock>
  offerings: Record<string, string[]>
  validation: Validation
  level_caps: LevelCap[]
  courses: Record<string, CourseMeta>
  error?: string
}

export interface SimCourse {
  code: string
  title: string | null
  units?: number | null
  level?: string | null
  semester?: string | null
  has_exam?: boolean | null
  offerings: string[]
}

export interface ScheduleResponse {
  semesters: { label: string; courses: { code: string }[]; units: number }[]
  unplaced: { code: string; reason: string }[]
  warnings: string[]
  courses?: Record<string, CourseMeta>
  error?: string
}

export interface AdviseCandidate {
  code: string
  title?: string | null
  offerings?: string[]
}

export interface AdviseResponse {
  advice?: string | null
  note?: string
  candidates?: AdviseCandidate[]
  offerings?: Record<string, string[]>
  unreachable_count?: number
  unreachable_codes?: string[]
  error?: string
}

export interface SimStateReq {
  program_id: string
  selected: string[]
  chosen_plans: string[]
  branch: string[]
  placement: Record<string, number>
  units_cap: number
  n_semesters: number
  start_sem: string
}

async function postJson<T>(
  url: string,
  body: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  })
  return r.json()
}

export async function fetchPrograms(): Promise<Program[]> {
  const r = await fetch('/api/sim/programs')
  return r.json()
}

export async function postSimState(req: SimStateReq): Promise<SimStateResponse> {
  return postJson<SimStateResponse>('/api/sim/state', req)
}

export async function getSimCourses(
  q: string,
  inProgram?: string,
): Promise<SimCourse[] | { error: string }> {
  const params = new URLSearchParams({ q })
  if (inProgram) params.set('in_program', inProgram)
  const r = await fetch('/api/sim/courses?' + params)
  return r.json()
}

export async function postSimSchedule(req: {
  program_id: string
  selected: string[]
  chosen_plans: string[]
  units_cap: number
  start_sem: string
}): Promise<ScheduleResponse> {
  return postJson<ScheduleResponse>('/api/sim/schedule', req)
}

export async function postSimAdvise(req: {
  program_id: string
  selected: string[]
  chosen_plans: string[]
  branch: string[]
  goal: string
}): Promise<AdviseResponse> {
  return postJson<AdviseResponse>('/api/sim/advise', req, await turnstileHeaders())
}
