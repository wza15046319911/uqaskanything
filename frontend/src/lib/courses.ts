import type { Course } from '../api/ask'
import i18n from '../i18n'

export interface Slot {
  group: boolean
  members: Course[]
}

export function levelZh(level: string | null | undefined): string {
  if (level === 'Postgraduate Coursework') return i18n.t('common.postgrad')
  if (level === 'Undergraduate') return i18n.t('common.undergrad')
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
