import type { Course } from '../api/ask'

export interface Slot {
  group: boolean
  members: Course[]
}

const CN_NUM: Record<number, string> = {
  2: '二',
  3: '三',
  4: '四',
  5: '五',
  6: '六',
  7: '七',
  8: '八',
  9: '九',
  10: '十',
}

export function cnNum(k: number): string {
  return CN_NUM[k] ?? String(k)
}

export function levelZh(level: string | null | undefined): string {
  if (level === 'Postgraduate Coursework') return '研究生'
  if (level === 'Undergraduate') return '本科'
  return level ?? ''
}

// Collapse an equivalence (pick-one-of-two) group into one slot by (course_list, equiv_group);
// standalone courses each get their own slot. filter/semantic courses have no equiv_group, so they are returned one by one.
export function collapseSlots(rows: Course[]): Slot[] {
  const slots: Slot[] = []
  const groups: Record<string, Slot> = {}
  for (const c of rows) {
    const g = c.equiv_group || ''
    if (!g) {
      slots.push({ group: false, members: [c] })
      continue
    }
    const key = `${c.course_list || ''} ${g}`
    let s = groups[key]
    if (!s) {
      s = { group: true, members: [] }
      groups[key] = s
      slots.push(s)
    }
    s.members.push(c)
  }
  return slots
}
