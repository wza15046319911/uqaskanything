// Diffusion background param controls: three sliders for hue / softness / grain + reshuffle (random seed).
// The sim export preview and the cover generator share the same knobs; the enabled switch and fade level are kept by each container.

import { Button, Label, Slider } from '@heroui/react'

export interface DiffusionParams {
  colorMix: number
  softness: number
  texture: number
  seed: number
}

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
}

export default function DiffusionControls({ value, onChange, disabled }: DiffusionControlsProps) {
  const reshuffle = () => onChange({ ...value, seed: Math.floor(Math.random() * 900000) })
  return (
    <div className="flex flex-col gap-3">
      <DiffRange
        label="色相"
        value={value.colorMix}
        disabled={disabled}
        onChange={(v) => onChange({ ...value, colorMix: v })}
      />
      <DiffRange
        label="柔度"
        value={value.softness}
        disabled={disabled}
        onChange={(v) => onChange({ ...value, softness: v })}
      />
      <DiffRange
        label="颗粒"
        value={value.texture}
        disabled={disabled}
        onChange={(v) => onChange({ ...value, texture: v })}
      />
      <Button size="sm" variant="secondary" onPress={reshuffle} isDisabled={disabled}>
        换一张
      </Button>
    </div>
  )
}
