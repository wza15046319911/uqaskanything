import { useState } from 'react'
import Results from './components/Results'
import { fetchAsk, type AskResult } from './api/ask'

const EXAMPLES = [
  '跟机器学习相关、没有考试的课',
  '有哪些2学分的研究生课程',
  'CSSE1001是哪些专业的必修',
  '想了解网络安全有哪些课',
  'Bachelor of Computer Science 要修哪些核心课',
]

export default function App() {
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [res, setRes] = useState<AskResult | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const ask = async (question?: string) => {
    const text = (question ?? q).trim()
    if (!text || loading) return
    if (question) setQ(question)
    setLoading(true)
    setErr(null)
    setRes(null)
    try {
      const data = await fetchAsk(text)
      if (data.error) setErr(data.error)
      else setRes(data)
    } catch (e) {
      setErr(`连不上服务:${e instanceof Error ? e.message : String(e)}(确认后端 uvicorn 正在运行)`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="wrap">
      <header>
        <span className="badge">
          <span className="dot"></span>本地 RAG · 1508 门课 · 335 个专业
        </span>
        <h1>
          问问 UQ 的<em>课</em>
        </h1>
        <p className="sub">用大白话提问 —— 找相关课程、筛有无考试、查某门课是哪些专业的必修。</p>
        <div className="searchcard">
          <svg
            className="lead"
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          >
            <circle cx="11" cy="11" r="7" />
            <path d="m21 21-4.3-4.3" />
          </svg>
          <input
            id="q"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && ask()}
            placeholder="比如:跟机器学习相关、没有考试的课"
            autoComplete="off"
            enterKeyHint="search"
          />
          <button id="ask" onClick={() => ask()} disabled={loading}>
            {loading ? (
              <span className="ask-spin"></span>
            ) : (
              <>
                <span className="ask-label">提问</span>
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </>
            )}
          </button>
        </div>
        <div className="chips">
          {EXAMPLES.map((t) => (
            <button key={t} className="chip" onClick={() => ask(t)}>
              {t}
            </button>
          ))}
        </div>
      </header>
      <main id="out" aria-live="polite">
        {loading && (
          <>
            <div className="skeleton"></div>
            <div className="skeleton"></div>
            <div className="skeleton"></div>
            <div className="skeleton"></div>
          </>
        )}
        {!loading && err && <div className="note err">出错了:{err}</div>}
        {!loading && !err && res && <Results res={res} />}
      </main>
      <footer>本地 qwen2.5-coder + bge-m3 · 数据为 S1 / St Lucia / In Person</footer>
    </div>
  )
}
