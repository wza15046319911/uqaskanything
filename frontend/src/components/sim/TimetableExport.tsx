// 模拟器右侧的「导出版面」:既作屏幕预览,也供 modern-screenshot 导出图片。
// 沿用十六进制颜色与内联样式,配色映射与屏幕卡片共用 sim-sections。
// 底图为 OpenAI 风格弥散渐变(diffusion-bg),内容浮在白色面板上保证可读性。

import { semKind, semYear, type SimLocalState } from '../../lib/sim'
import { sectionOf, type SectionMap } from '../../lib/sim-sections'
import type { SimStateResponse } from '../../api/sim'

interface DiffusionParams {
  enabled: boolean
  colorMix: number
  softness: number
  texture: number
  seed: number
}

interface TimetableExportProps {
  state: SimLocalState
  data: SimStateResponse
  sectionMap: SectionMap
  diffusion: DiffusionParams
  bg: string // 预算好的弥散底图 dataURL(父组件统一计算,两个节点共用)
  coreOnly: boolean
}

const INK = '#2c2c2a'
const MUTED = '#6b6b78'
const LINE = '#e6e3f0'
const UQ_DEEP = '#26215c'
const FONT =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'

export default function TimetableExport({
  state,
  data,
  sectionMap,
  diffusion,
  bg,
  coreOnly,
}: TimetableExportProps) {
  const visible = (c: string) => !coreOnly || sectionOf(sectionMap, c).isCore
  const placedBy: Record<number, string[]> = {}
  for (const [c, i] of Object.entries(state.placement)) {
    if (!visible(c)) continue
    ;(placedBy[i] = placedBy[i] || []).push(c)
  }
  const legend = coreOnly ? sectionMap.legend.filter((s) => s.isCore) : sectionMap.legend
  const ctitle = (c: string) => data.courses[c]?.title || '(无开课信息)'
  const cunits = (c: string) => data.courses[c]?.units
  // 弥散开启时面板/格子半透明,让背景透上来;课卡更不透明保证文字可读。
  const panelBg = diffusion.enabled ? 'rgba(255, 255, 255, 0.5)' : '#ffffff'
  const cellBg = diffusion.enabled ? 'rgba(255, 255, 255, 0.5)' : '#fbfbfd'

  return (
    <div
      style={{
        width: 1200,
        padding: diffusion.enabled ? 48 : 0,
        backgroundColor: diffusion.enabled ? '#0f1530' : '#ffffff',
        backgroundImage: bg ? `url(${bg})` : undefined,
        backgroundSize: 'cover',
        backgroundPosition: 'center',
        fontFamily: FONT,
        color: INK,
        boxSizing: 'border-box',
      }}
    >
      <div
        style={{
          background: panelBg,
          borderRadius: diffusion.enabled ? 20 : 0,
          padding: 32,
          boxShadow: diffusion.enabled ? '0 18px 50px rgba(15, 18, 45, 0.28)' : 'none',
          boxSizing: 'border-box',
        }}
      >
        <div style={{ marginBottom: 24 }}>
          <div style={{ fontSize: 26, fontWeight: 700, color: UQ_DEEP, lineHeight: 1.2 }}>
            {data.title}
          </div>
          {/* <div style={{ fontSize: 14, color: MUTED, marginTop: 6 }}>
            {state.start_sem} {state.start_year} 入学 · 共 {state.years} 年 · 每学期上限{' '}
            {state.units_cap} 学分
          </div> */}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {Array.from({ length: Math.ceil(state.n_semesters / 2) }, (_, y) => (
            <div
              key={y}
              style={{
                display: 'grid',
                gridTemplateColumns: '64px minmax(0, 1fr) minmax(0, 1fr)',
                gap: 12,
                alignItems: 'stretch',
              }}
            >
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 700,
                  color: MUTED,
                  paddingTop: 6,
                }}
              >
                Year {y + 1}
              </div>
              {[2 * y, 2 * y + 1].map((i) => {
                if (i >= state.n_semesters) return null
                const kind = semKind(state.start_sem, i)
                const year = semYear(state.start_year, state.start_sem, i)
                const codes = placedBy[i] || []
                const u = coreOnly
                  ? codes.reduce((a, c) => a + (cunits(c) || 0), 0)
                  : data.validation.semester_units?.[i] || 0
                return (
                  <div
                    key={i}
                    style={{
                      minWidth: 0,
                      border: `1px solid ${LINE}`,
                      borderRadius: 12,
                      padding: 12,
                      background: cellBg,
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'baseline',
                        marginBottom: 8,
                      }}
                    >
                      <span style={{ fontSize: 13, fontWeight: 700, color: UQ_DEEP }}>
                        {kind} {year}
                      </span>
                      <span style={{ fontSize: 12, color: MUTED }}>{u} 学分</span>
                    </div>
                    {codes.length === 0 && (
                      <div style={{ fontSize: 12, color: '#b6b6c0', padding: '6px 0' }}>—</div>
                    )}
                    {codes.map((c) => {
                      const sec = sectionOf(sectionMap, c)
                      const un = cunits(c)
                      return (
                        <div
                          key={c}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            borderLeft: `4px solid ${sec.color}`,
                            background: diffusion.enabled
                              ? 'rgba(255, 255, 255, 0.7)'
                              : sec.color + '1a',
                            borderRadius: 8,
                            padding: '7px 10px',
                            marginBottom: 6,
                          }}
                        >
                          <span
                            style={{
                              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                              fontSize: 13,
                              fontWeight: 700,
                              color: INK,
                              flexShrink: 0,
                            }}
                          >
                            {c}
                          </span>
                          <span
                            style={{
                              fontSize: 12.5,
                              color: INK,
                              flex: 1,
                              minWidth: 0,
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {ctitle(c)}
                          </span>
                          {un != null && (
                            <span style={{ fontSize: 11.5, color: MUTED, flexShrink: 0 }}>
                              {un} units
                            </span>
                          )}
                        </div>
                      )
                    })}
                  </div>
                )
              })}
            </div>
          ))}
        </div>

        {legend.length > 0 && (
          <div
            style={{
              display: 'flex',
              flexWrap: 'wrap',
              gap: 16,
              marginTop: 24,
              paddingTop: 16,
              borderTop: `1px solid ${LINE}`,
            }}
          >
            {legend.map((s) => (
              <div key={s.ref} style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                <span
                  style={{
                    width: 14,
                    height: 14,
                    borderRadius: 4,
                    background: s.color,
                    flexShrink: 0,
                  }}
                />
                <span style={{ fontSize: 13, color: INK }}>{s.title}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
