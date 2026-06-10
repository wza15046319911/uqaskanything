import { useState } from 'react'
import { fetchAsk, type AskResult } from './api/ask'

export default function App() {
  const [q, setQ] = useState('')
  const [loading, setLoading] = useState(false)
  const [res, setRes] = useState<AskResult | null>(null)

  const ask = async () => {
    if (!q.trim()) return
    setLoading(true)
    setRes(null)
    try {
      setRes(await fetchAsk(q))
    } catch (e) {
      setRes({ error: String(e) })
    } finally {
      setLoading(false)
    }
  }

  return (
    <main
      style={{
        maxWidth: 720,
        margin: '40px auto',
        padding: '0 16px',
        fontFamily: 'system-ui, sans-serif',
      }}
    >
      <h1>UQ 课程问答</h1>
      <p style={{ color: '#666' }}>
        脚手架已就绪。开发期 /api 通过 Vite 代理转发到 FastAPI(:8077)。
      </p>
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && ask()}
          placeholder="例:CS 有哪些课程没有考试"
          style={{ flex: 1, padding: 8, fontSize: 16 }}
        />
        <button onClick={ask} disabled={loading} style={{ padding: '8px 16px' }}>
          {loading ? '查询中…' : '提问'}
        </button>
      </div>
      {res && (
        <section style={{ marginTop: 24 }}>
          {res.error ? (
            <p style={{ color: 'crimson' }}>错误:{res.error}</p>
          ) : (
            <>
              {res.answer && <p style={{ lineHeight: 1.7 }}>{res.answer}</p>}
              {res.courses && res.courses.length > 0 && (
                <ul>
                  {res.courses.map((c) => (
                    <li key={c.code}>
                      <b>{c.code}</b> {c.title}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </section>
      )}
    </main>
  )
}
