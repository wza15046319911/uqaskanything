// 选课模拟器配色:把已排课程按培养方案需求组映射到固定颜色。
// 确定性逻辑、无 LLM;屏幕卡片与导出图共用这一份调色板。
// 链路:课程码 -> selected_by_rule 找归属 rule -> child_of 上溯顶层组 -> 调色板。
// 既不在任何 rule 也不在 unattributed 的边界码,显式归「未计入」,不静默。

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

export const UNATTRIBUTED_SECTION: Section = {
  ref: '__unattributed__',
  title: '未计入',
  color: UNATTRIBUTED_COLOR,
  isCore: false,
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
      codeToSection[c] = UNATTRIBUTED_SECTION
      usedUnatt = true
    }
  }

  const legend: Section[] = data.rules
    .filter((r) => !r.child_of && usedTops.has(r.ref))
    .map((r) => sectionByTop[r.ref])
  if (usedUnatt) legend.push(UNATTRIBUTED_SECTION)

  return { codeToSection, legend }
}

export function sectionOf(map: SectionMap, code: string): Section {
  return map.codeToSection[code] || UNATTRIBUTED_SECTION
}
