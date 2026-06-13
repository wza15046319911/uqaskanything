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
  // course_to_programs 时是数组;permit 等场景是对象;空时 null
  program_facts?: ProgramFact[] | Record<string, unknown> | null
  chunks?: KbChunk[]
  course?: CourseDetail | null
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

// 流式问答:meta(结构化课程,一次) -> token(答案增量,多次) -> done(护栏后全文)
export interface AskMeta {
  mode?: AskMode
  meta?: string
  courses?: Course[]
  program_facts?: ProgramFact[] | Record<string, unknown> | null
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
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, generate: true }),
    })
  } catch (e) {
    h.onError(`连不上服务:${e instanceof Error ? e.message : String(e)}(确认后端 uvicorn 正在运行)`)
    return
  }
  if (!r.ok || !r.body) {
    h.onError(`服务异常 (HTTP ${r.status})`)
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
    buf = chunks.pop() ?? '' // 末尾不完整事件留到下一轮
    for (const chunk of chunks) {
      const line = chunk.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      const payload = line.slice(line.indexOf(':') + 1).trim()
      if (!payload) continue
      let msg: { type: string; data: unknown }
      try {
        msg = JSON.parse(payload)
      } catch {
        continue // 半截 JSON(理论上被 \n\n 切分挡住,这里兜底)
      }
      if (msg.type === 'meta') h.onMeta(msg.data as AskMeta)
      else if (msg.type === 'token') h.onToken(msg.data as string)
      else if (msg.type === 'done') h.onDone(msg.data as string)
      else if (msg.type === 'error') h.onError(msg.data as string)
    }
  }
}
