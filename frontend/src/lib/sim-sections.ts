// Course planning simulator colors: map placed courses to fixed colors by the program requirement group.
// Deterministic logic, no LLM; screen cards and the export image share this one palette.
// Chain: course code -> find the owning rule in selected_by_rule -> walk up child_of to the top group -> palette.
// A boundary code that is in no rule and not in unattributed is explicitly put under "not counted", never silently.

import i18n from '../i18n'
import type { SimStateResponse } from '../api/sim'

export interface Section {
  ref: string
  title: string
  color: string
  isCore: boolean
}

const CORE_COLOR = '#3c3489'
const UNATTRIBUTED_COLOR = '#9aa0ab'
const ELECTIVE_PALETTE = ['#5b8ff0', '#3fa66a', '#e0852b', '#d64a3f', '#8c5bb0', '#1fa2a6']

function unattributedSection(): Section {
  return {
    ref: '__unattributed__',
    title: i18n.t('sim.notCounted'),
    color: UNATTRIBUTED_COLOR,
    isCore: false,
  }
}

export interface SectionMap {
  codeToSection: Record<string, Section>
  legend: Section[]
}

export function buildSectionMap(data: SimStateResponse, placed: string[]): SectionMap {
  const byRef: Record<string, SimStateResponse['rules'][number]> = {}
  for (const r of data.rules) byRef[r.ref] = r

  const topRefOf = (ref: string): string => {
    let cur = ref
    const seen = new Set<string>()
    while (byRef[cur]?.child_of && !seen.has(cur)) {
      seen.add(cur)
      cur = byRef[cur].child_of as string
    }
    return cur
  }

  const sectionByTop: Record<string, Section> = {}
  let pal = 0
  for (const r of data.rules) {
    if (r.child_of) continue
    const color =
      r.select_type === 'all' ? CORE_COLOR : ELECTIVE_PALETTE[pal++ % ELECTIVE_PALETTE.length]
    sectionByTop[r.ref] = {
      ref: r.ref,
      title: r.title || r.ref,
      color,
      isCore: r.select_type === 'all',
    }
  }

  const topByCode: Record<string, string> = {}
  for (const [ref, codes] of Object.entries(data.selected_by_rule || {})) {
    const top = topRefOf(ref)
    for (const c of codes) topByCode[c] = top
  }

  const codeToSection: Record<string, Section> = {}
  const usedTops = new Set<string>()
  let usedUnatt = false
  for (const c of placed) {
    const top = topByCode[c]
    const sec = top ? sectionByTop[top] : undefined
    if (sec) {
      codeToSection[c] = sec
      usedTops.add(top)
    } else {
      codeToSection[c] = unattributedSection()
      usedUnatt = true
    }
  }

  const legend: Section[] = data.rules
    .filter((r) => !r.child_of && usedTops.has(r.ref))
    .map((r) => sectionByTop[r.ref])
  if (usedUnatt) legend.push(unattributedSection())

  return { codeToSection, legend }
}

export function sectionOf(map: SectionMap, code: string): Section {
  return map.codeToSection[code] || unattributedSection()
}
