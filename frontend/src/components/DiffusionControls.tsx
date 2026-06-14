// 弥散背景参数控件:色相 / 柔度 / 颗粒 三档滑块 + 换一张(随机 seed)。
// sim 导出预览与封面生成器共用同一套旋钮;enabled 开关、淡化浓度由各自容器维护。

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
