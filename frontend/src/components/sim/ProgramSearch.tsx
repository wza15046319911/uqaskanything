import { useState, type KeyboardEvent } from 'react'
import type { Program } from '../../api/sim'

interface ProgramSearchProps {
  programs: Program[]
  current?: Program
  onPick: (id: string) => void
}

export default function ProgramSearch({ programs, current, onPick }: ProgramSearchProps) {
  const [q, setQ] = useState('')
  const [hi, setHi] = useState(0)

  const ql = q.trim().toLowerCase()
  const hits = ql
    ? programs
        .filter((p) => p.title.toLowerCase().includes(ql) || p.program_id.includes(ql))
        .slice(0, 30)
    : []

  const pick = (id: string) => {
    setQ('')
    onPick(id)
  }

  const onKey = (e: KeyboardEvent) => {
    if (!hits.length) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setHi((h) => Math.min(h + 1, hits.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setHi((h) => Math.max(h - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      pick((hits[hi] || hits[0]).program_id)
    } else if (e.key === 'Escape') {
      setQ('')
    }
  }

  return (
    <div className="search">
      <input
        value={q}
        onChange={(e) => {
          setQ(e.target.value)
          setHi(0)
        }}
        onKeyDown={onKey}
        placeholder="搜专业,如 Computer Science…"
        autoComplete="off"
      />
      {hits.length > 0 && (
        <div className="drop on">
          {hits.map((p, i) => (
            <div
              key={p.program_id}
              className={`opt${i === hi ? ' hi' : ''}`}
              onMouseEnter={() => setHi(i)}
              onClick={() => pick(p.program_id)}
            >
              {p.title}
              <small>
                {p.program_id} · {p.total_units}u
              </small>
            </div>
          ))}
        </div>
      )}
      {current && (
        <div className="curprog">
          当前:<b>{current.title}</b>{' '}
          <small>
            ({current.program_id} · {current.total_units}学分)
          </small>
        </div>
      )}
    </div>
  )
}
