export interface Course {
  code: string
  title: string
}

export interface AskResult {
  mode?: string
  answer?: string | null
  courses?: Course[]
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
