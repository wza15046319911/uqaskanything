import { useState } from 'react'
import { ComboBox, Input, ListBox } from '@heroui/react'
import type { Program } from '../../api/sim'

interface ProgramSearchProps {
  programs: Program[]
  current?: Program
  onPick: (id: string) => void
}

export default function ProgramSearch({ programs, current, onPick }: ProgramSearchProps) {
  const [q, setQ] = useState('')

  const ql = q.trim().toLowerCase()
  const hits = ql
    ? programs
        .filter((p) => p.title.toLowerCase().includes(ql) || p.program_id.includes(ql))
        .slice(0, 30)
    : []

  return (
    <div className="mx-auto mt-4 max-w-md text-left">
      <ComboBox
        aria-label="搜专业,如 Computer Science…"
        inputValue={q}
        onInputChange={setQ}
        selectedKey={null}
        onSelectionChange={(key: string | number | null) => {
          if (key != null) {
            setQ('')
            onPick(String(key))
          }
        }}
        items={hits}
        allowsCustomValue
        menuTrigger="input"
      >
        <ComboBox.InputGroup>
          <Input placeholder="搜专业,如 Computer Science…" autoComplete="off" />
          <ComboBox.Trigger />
        </ComboBox.InputGroup>
        <ComboBox.Popover>
          <ListBox>
            {(p: Program) => (
              <ListBox.Item id={p.program_id} textValue={p.title}>
                <span className="min-w-0 flex-1 truncate">{p.title}</span>
                <span className="shrink-0 text-xs text-muted tabular-nums">
                  {p.program_id} · {p.total_units}u
                </span>
              </ListBox.Item>
            )}
          </ListBox>
        </ComboBox.Popover>
      </ComboBox>
      {current && (
        <div className="mt-2.5 text-center text-[13px] text-muted">
          当前:<b className="font-semibold text-foreground">{current.title}</b>{' '}
          {/* <span className="text-xs">
            ({current.program_id} · {current.total_units}学分)
          </span> */}
        </div>
      )}
    </div>
  )
}
