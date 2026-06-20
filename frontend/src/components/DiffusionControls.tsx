// Full diffusion panel ported from OpenAI Gradient Atelier: preset selector (7), seven sliders, frame switch, randomize,
// evolution lab (10 candidates), and PNG export. Emits a complete DiffusionParams; the host owns the enabled switch and the
// fade level. The sim export preview and the cover generator share this one panel.

import { useDeferredValue, useMemo, useState } from 'react'
import { Button, Label, Slider } from '@heroui/react'
import { useTranslation } from 'react-i18next'
import {
  dimensionsFor,
  generateCandidates,
  PRESET_DEFAULTS,
  renderDiffusionDataUrl,
  type DiffusionCandidate,
  type DiffusionFrame,
  type DiffusionPreset,
  type DiffusionSettings,
} from '../lib/diffusion-bg'

export interface DiffusionParams extends DiffusionSettings {
  preset: DiffusionPreset
  frame: DiffusionFrame
  seed: number
}

const PRESETS: DiffusionPreset[] = [
  'diffusion',
  'horizon',
  'aurora',
  'prism',
  'watercolor',
  'material',
  'mono',
]

const FRAMES: { id: DiffusionFrame; label: string }[] = [
  { id: 'square', label: '1:1' },
  { id: 'wide', label: '16:9' },
  { id: 'card', label: '' },
]

interface DiffRangeProps {
  label: string
  value: number
  disabled?: boolean
  onChange: (v: number) => void
}

function DiffRange({ label, value, disabled, onChange }: DiffRangeProps) {
  return (
    <Slider
      value={value}
      minValue={0}
      maxValue={100}
      isDisabled={disabled}
      onChange={(v) => onChange(v as number)}
    >
      <div className="flex items-center justify-between text-[11px] text-muted">
        <Label>{label}</Label>
        <Slider.Output className="tabular-nums" />
      </div>
      <Slider.Track>
        <Slider.Fill />
        <Slider.Thumb />
      </Slider.Track>
    </Slider>
  )
}

interface DiffusionControlsProps {
  value: DiffusionParams
  onChange: (next: DiffusionParams) => void
  disabled?: boolean
  // When set (width / height), the preview renders at this aspect instead of the frame, so it matches a host whose output ratio is fixed.
  previewAspect?: number
}

export default function DiffusionControls({
  value,
  onChange,
  disabled,
  previewAspect,
}: DiffusionControlsProps) {
  const { t } = useTranslation()
  const [candidates, setCandidates] = useState<DiffusionCandidate[]>([])
  const [activeSeed, setActiveSeed] = useState<number | null>(null)

  const segClass = (active: boolean) =>
    `min-w-0 truncate rounded-lg border px-2 py-1.5 text-[12px] transition-colors ${
      active
        ? 'border-accent bg-accent-soft text-foreground'
        : 'border-border bg-background text-muted'
    } disabled:opacity-50`

  const selectPreset = (preset: DiffusionPreset) =>
    onChange({ ...value, preset, ...PRESET_DEFAULTS[preset] })

  const selectFrame = (frame: DiffusionFrame) => onChange({ ...value, frame })

  const randomize = () =>
    onChange({
      ...value,
      seed: Math.floor(Math.random() * 900000) + 1000,
      colorMix: Math.floor(Math.random() * 101),
      softness: Math.floor(45 + Math.random() * 50),
      texture: Math.floor(20 + Math.random() * 68),
      materialDepth: Math.floor(Math.random() * 88),
      bands: Math.floor(Math.random() * 95),
      brush: Math.floor(12 + Math.random() * 82),
      vignette: Math.floor(Math.random() * 55),
    })

  const reshuffle = () => onChange({ ...value, seed: Math.floor(Math.random() * 900000) })

  const generate = () => {
    setCandidates(generateCandidates(value, value.seed))
    setActiveSeed(null)
  }

  const applyCandidate = (c: DiffusionCandidate) => {
    setActiveSeed(c.seed)
    onChange({ ...value, ...c.settings, seed: c.seed })
  }

  const exportPng = () => {
    const dims = dimensionsFor(value.frame, 2160)
    const url = renderDiffusionDataUrl(dims.width, dims.height, value)
    const link = document.createElement('a')
    link.download = `gradient-atelier-${value.preset}-${value.seed}.png`
    link.href = url
    link.click()
  }

  const deferred = useDeferredValue(value)
  const previewUrl = useMemo(() => {
    const dims = previewAspect
      ? { width: Math.round(240 * previewAspect), height: 240 }
      : dimensionsFor(deferred.frame, 240)
    return renderDiffusionDataUrl(dims.width, dims.height, deferred)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    previewAspect,
    deferred.preset,
    deferred.frame,
    deferred.colorMix,
    deferred.softness,
    deferred.texture,
    deferred.materialDepth,
    deferred.bands,
    deferred.brush,
    deferred.vignette,
    deferred.seed,
  ])

  const candidateImages = useMemo(
    () =>
      candidates.map((c) => ({
        ...c,
        url: renderDiffusionDataUrl(220, 220, {
          ...c.settings,
          preset: value.preset,
          seed: c.seed,
        }),
      })),
    [candidates, value.preset],
  )
  const bestScore = candidates.reduce((m, c) => Math.max(m, c.score), 0)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span>{t('diffusion.mode')}</span>
          <button
            type="button"
            disabled={disabled}
            onClick={randomize}
            className="text-accent disabled:opacity-50"
          >
            {t('diffusion.randomize')}
          </button>
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          {PRESETS.map((p) => (
            <button
              key={p}
              type="button"
              disabled={disabled}
              onClick={() => selectPreset(p)}
              className={segClass(value.preset === p)}
            >
              {t(`diffusion.presets.${p}`)}
            </button>
          ))}
        </div>
      </div>

      <div className="flex justify-center">
        <img
          src={previewUrl}
          alt=""
          className="rounded-lg border border-border"
          style={{
            width: 120,
            aspectRatio: previewAspect
              ? String(previewAspect)
              : `${dimensionsFor(value.frame, 240).width} / ${dimensionsFor(value.frame, 240).height}`,
          }}
        />
      </div>

      <div className="flex flex-col gap-3">
        <DiffRange
          label={t('diffusion.hue')}
          value={value.colorMix}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, colorMix: v })}
        />
        <DiffRange
          label={t('diffusion.softness')}
          value={value.softness}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, softness: v })}
        />
        <DiffRange
          label={t('diffusion.grain')}
          value={value.texture}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, texture: v })}
        />
        <DiffRange
          label={t('diffusion.materialDepth')}
          value={value.materialDepth}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, materialDepth: v })}
        />
        <DiffRange
          label={t('diffusion.bands')}
          value={value.bands}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, bands: v })}
        />
        <DiffRange
          label={t('diffusion.brush')}
          value={value.brush}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, brush: v })}
        />
        <DiffRange
          label={t('diffusion.vignette')}
          value={value.vignette}
          disabled={disabled}
          onChange={(v) => onChange({ ...value, vignette: v })}
        />
      </div>

      <div className="flex flex-col gap-2">
        <span className="text-[11px] text-muted">{t('diffusion.frame')}</span>
        <div className="grid grid-cols-3 gap-1.5">
          {FRAMES.map((f) => (
            <button
              key={f.id}
              type="button"
              disabled={disabled}
              onClick={() => selectFrame(f.id)}
              className={segClass(value.frame === f.id)}
            >
              {f.label || t('diffusion.card')}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-2">
        <Button size="sm" variant="secondary" onPress={reshuffle} isDisabled={disabled}>
          {t('diffusion.reshuffle')}
        </Button>
        <Button size="sm" variant="secondary" onPress={exportPng} isDisabled={disabled}>
          {t('diffusion.exportPng')}
        </Button>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between text-[11px] text-muted">
          <span>{t('diffusion.evolutionLab')}</span>
          <span className="tabular-nums">
            {t('diffusion.best')} {bestScore.toFixed(2)}
          </span>
        </div>
        <Button size="sm" onPress={generate} isDisabled={disabled}>
          {t('diffusion.generateCandidates')}
        </Button>
        {candidateImages.length > 0 ? (
          <div className="grid grid-cols-2 gap-2">
            {candidateImages.map((c, i) => (
              <button
                key={c.seed}
                type="button"
                disabled={disabled}
                onClick={() => applyCandidate(c)}
                className={`rounded-lg border p-1 text-left transition-colors disabled:opacity-50 ${
                  activeSeed === c.seed ? 'border-accent' : 'border-border'
                }`}
              >
                <img
                  src={c.url}
                  alt=""
                  className="block w-full rounded"
                  style={{ aspectRatio: '1' }}
                />
                <span className="block pt-1 text-[10px] text-muted">
                  {String(i + 1).padStart(2, '0')} {t('diffusion.score')} {c.score.toFixed(2)}
                </span>
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  )
}
