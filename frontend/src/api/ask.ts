import i18n from '../i18n'
import { turnstileHeaders } from '../lib/turnstile'

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
  profile_url?: string | null
}

export interface ProgramFact {
  title: string
  requirement_type?: 'core' | 'elective' | null
  equiv_group?: string | null
  course_list?: string | null
  via_plan?: string | null
  plan_subtype?: string | null
}

// For program_to_courses / permit, program_facts is a single object (course_to_programs is ProgramFact[])
export interface ProgramAnswer {
  program: string
  program_id?: string
  requirement?: string
  course?: string
  excluded?: boolean
  in_program?: boolean
  filter?: string
}

export interface KbChunk {
  url: string
  page_title?: string | null
  breadcrumb?: string | null
  source_type?: string | null
}

export interface CourseDetail {
  code: string
  title?: string | null
  units?: number | null
  level?: string | null
  prerequisite_raw?: string | null
  incompatible?: string | null
  has_exam?: boolean | null
  has_hurdle?: boolean | null
  semesters?: string[]
  locations?: string[]
  profile_url: string
}

export type AskMode =
  | 'filter'
  | 'semantic'
  | 'hybrid'
  | 'program'
  | 'kb'
  | 'course_detail'
  | 'empty'

export interface AskResult {
  mode?: AskMode
  answer?: string | null
  courses?: Course[]
  // It is an array for course_to_programs; an object for permit and similar; null when empty
  program_facts?: ProgramFact[] | ProgramAnswer | null
  chunks?: KbChunk[]
  course?: CourseDetail | null
  meta?: string
  error?: string
}

export async function fetchAsk(question: string): Promise<AskResult> {
  const r = await fetch('/api/ask', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await turnstileHeaders()) },
    body: JSON.stringify({ question, generate: true }),
  })
  return r.json()
}

// Streaming Q&A: meta (structured courses, once) -> token (answer increments, many times) -> done (full text after guardrails)
export interface AskMeta {
  mode?: AskMode
  meta?: string
  courses?: Course[]
  program_facts?: ProgramFact[] | ProgramAnswer | null
  chunks?: KbChunk[]
  course?: CourseDetail | null
}

export interface AskStreamHandlers {
  onMeta: (meta: AskMeta) => void
  onToken: (delta: string) => void
  onDone: (fullAnswer: string) => void
  onError: (message: string) => void
}

export async function fetchAskStream(question: string, h: AskStreamHandlers): Promise<void> {
  let r: Response
  try {
    r = await fetch('/api/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(await turnstileHeaders()) },
      body: JSON.stringify({ question, generate: true }),
    })
  } catch (e) {
    h.onError(i18n.t('ask.connectFail', { msg: e instanceof Error ? e.message : String(e) }))
    return
  }
  if (!r.ok || !r.body) {
    h.onError(i18n.t('ask.serverError', { status: r.status }))
    return
  }

  const reader = r.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const chunks = buf.split('\n\n')
    buf = chunks.pop() ?? '' // keep the incomplete trailing event for the next round
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      const payload = line.slice(line.indexOf(':') + 1).trim()
      if (!payload) continue
      let msg: { type: string; data: unknown }
      try {
        msg = JSON.parse(payload)
      } catch {
        continue // partial JSON (in theory the \n\n split blocks this; this is a fallback)
      }
      if (msg.type === 'meta') h.onMeta(msg.data as AskMeta)
      else if (msg.type === 'token') h.onToken(msg.data as string)
      else if (msg.type === 'done') h.onDone(msg.data as string)
      else if (msg.type === 'error') h.onError(msg.data as string)
    }
  }
}
