export interface Course {
  code: string
  title: string | null
  units?: number | null
  level?: string | null
  semester?: string | null
  has_exam?: boolean | null
  requirement_type?: 'core' | 'elective' | null
  equiv_group?: string | null
  course_list?: string | null
  sim?: number | null
}

export interface ProgramFact {
  title: string
  requirement_type?: 'core' | 'elective' | null
  equiv_group?: string | null
  course_list?: string | null
  via_plan?: string | null
  plan_subtype?: string | null
}

export type AskMode = 'filter' | 'semantic' | 'hybrid' | 'program' | 'empty'

export interface AskResult {
  mode?: AskMode
  answer?: string | null
  courses?: Course[]
  // course_to_programs 时是数组;permit 等场景是对象;空时 null
  program_facts?: ProgramFact[] | Record<string, unknown> | null
  meta?: string
  error?: string
}

export async function fetchAsk(question: string): Promise<AskResult> {
  const r = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, generate: true }),
  })
  return r.json()
}
