// OpenAI-style "diffusion" gradient background generator: a full port of the local tool OpenAI Gradient Atelier.
// All seven presets are ported one-to-one (diffusion / horizon / aurora / prism / watercolor / material / mono), keeping the original palettes,
// random-number consumption order, and layer pipeline. renderTo dispatches by preset: drawBase -> preset-specific layer -> drawBrush (skipped for
// watercolor/aurora) -> drawHorizon (skipped for horizon) -> drawMaterial (x0.45) -> drawVignette -> drawTexture. Outputs a PNG dataURL.
// Also exports preset defaults, frame sizing, and the evolution-lab helpers (scoreCandidate / generateCandidates) so the shared control can drive them.
// Use: laid under the sim/cover export image. Deterministic (fixed seed) -> preview, candidates, and download all match.

export type DiffusionPreset =
  | 'diffusion'
  | 'horizon'
  | 'aurora'
  | 'prism'
  | 'watercolor'
  | 'material'
  | 'mono'

export type DiffusionFrame = 'square' | 'wide' | 'card'

const palettes: string[][] = [
  ['#166ed1', '#56d3df', '#4961dc', '#d2f5f2', '#203060'],
  ['#6b55d8', '#40bfe8', '#b16ef0', '#84f2eb', '#1f2370'],
  ['#f55d9e', '#ff9c54', '#ffe8a8', '#c5d6ff', '#5f43c8'],
  ['#15a8d8', '#7bdd74', '#0089d6', '#d4f8cf', '#145c9f'],
  ['#ffb100', '#f87635', '#f7e178', '#9ed8cc', '#fff1c6'],
  ['#8d65ff', '#d97af5', '#a8c9ff', '#f2d5ff', '#6941bf'],
  ['#e8d8ef', '#ff66d0', '#1730ff', '#1c2731', '#f7eef7'],
]

interface Rgb {
  r: number
  g: number
  b: number
}

export interface DiffusionSettings {
  colorMix: number // 0-100, interpolate the color between palettes
  softness: number // glow spread
  texture: number // grain intensity 0-100
  materialDepth: number // material glow intensity 0-100
  bands: number // horizon band intensity 0-100 (<4 not drawn)
  brush: number // brush intensity 0-100
  vignette: number // vignette 0-100
}

export interface DiffusionOptions extends Partial<DiffusionSettings> {
  preset?: DiffusionPreset
  seed?: number
}

export const PRESET_DEFAULTS: Record<DiffusionPreset, DiffusionSettings> = {
  diffusion: {
    colorMix: 30,
    softness: 78,
    texture: 36,
    materialDepth: 28,
    bands: 0,
    brush: 24,
    vignette: 20,
  },
  horizon: {
    colorMix: 66,
    softness: 84,
    texture: 58,
    materialDepth: 22,
    bands: 0,
    brush: 18,
    vignette: 42,
  },
  aurora: {
    colorMix: 94,
    softness: 86,
    texture: 56,
    materialDepth: 30,
    bands: 0,
    brush: 18,
    vignette: 38,
  },
  prism: {
    colorMix: 16,
    softness: 88,
    texture: 34,
    materialDepth: 46,
    bands: 0,
    brush: 12,
    vignette: 18,
  },
  watercolor: {
    colorMix: 78,
    softness: 42,
    texture: 68,
    materialDepth: 14,
    bands: 0,
    brush: 88,
    vignette: 14,
  },
  material: {
    colorMix: 18,
    softness: 54,
    texture: 34,
    materialDepth: 82,
    bands: 0,
    brush: 42,
    vignette: 38,
  },
  mono: {
    colorMix: 44,
    softness: 64,
    texture: 62,
    materialDepth: 18,
    bands: 0,
    brush: 16,
    vignette: 24,
  },
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t
}

function hexToRgb(hex: string): Rgb {
  const v = hex.replace('#', '')
  return {
    r: parseInt(v.slice(0, 2), 16),
    g: parseInt(v.slice(2, 4), 16),
    b: parseInt(v.slice(4, 6), 16),
  }
}

function rgbToCss(rgb: Rgb, alpha = 1): string {
  return `rgba(${Math.round(rgb.r)}, ${Math.round(rgb.g)}, ${Math.round(rgb.b)}, ${alpha})`
}

function rgbToHex(rgb: Rgb): string {
  const to = (v: number) => clamp(Math.round(v), 0, 255).toString(16).padStart(2, '0')
  return `#${to(rgb.r)}${to(rgb.g)}${to(rgb.b)}`
}

// The tone a diffusion background reads as: the average of the three base-gradient stops (0/0.44/1 fill
// the whole canvas). Averaging tracks the background's real vividness — a soft, multi-hue mix averages to a
// muted color, a clean single-family gradient stays saturated — so the cover decoration matches its intensity,
// not just its hue.
export function dominantHex(colorMix: number): string {
  const stops = paletteAt(colorMix).slice(0, 3)
  const avg = stops.reduce(
    (acc, c) => ({ r: acc.r + c.r / 3, g: acc.g + c.g / 3, b: acc.b + c.b / 3 }),
    { r: 0, g: 0, b: 0 },
  )
  return rgbToHex(avg)
}

function mixHex(a: string, b: string, t: number): Rgb {
  const ca = hexToRgb(a)
  const cb = hexToRgb(b)
  return { r: lerp(ca.r, cb.r, t), g: lerp(ca.g, cb.g, t), b: lerp(ca.b, cb.b, t) }
}

function paletteAt(progress: number): Rgb[] {
  const scaled = (progress / 100) * (palettes.length - 1)
  const index = Math.floor(scaled)
  const next = Math.min(index + 1, palettes.length - 1)
  const local = scaled - index
  return palettes[index].map((color, i) => mixHex(color, palettes[next][i], local))
}

function mulberry32(seed: number): () => number {
  let value = seed >>> 0
  return function random() {
    value += 0x6d2b79f5
    let t = value
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function addRadial(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  radius: number,
  inner: Rgb,
  outer: Rgb,
  alpha: number,
): void {
  const g = ctx.createRadialGradient(x, y, 0, x, y, radius)
  g.addColorStop(0, rgbToCss(inner, alpha))
  g.addColorStop(0.55, rgbToCss(outer, alpha * 0.38))
  g.addColorStop(1, rgbToCss(outer, 0))
  ctx.fillStyle = g
  ctx.fillRect(0, 0, ctx.canvas.width, ctx.canvas.height)
}

function addEllipticalGlow(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  radiusX: number,
  radiusY: number,
  inner: Rgb,
  outer: Rgb,
  alpha: number,
): void {
  const scaleY = radiusY / Math.max(radiusX, 1)
  ctx.save()
  ctx.translate(x, y)
  ctx.scale(1, scaleY)
  const g = ctx.createRadialGradient(0, 0, 0, 0, 0, radiusX)
  g.addColorStop(0, rgbToCss(inner, alpha))
  g.addColorStop(0.5, rgbToCss(outer, alpha * 0.34))
  g.addColorStop(1, rgbToCss(outer, 0))
  ctx.fillStyle = g
  ctx.fillRect(
    -ctx.canvas.width * 2,
    -ctx.canvas.height * 2,
    ctx.canvas.width * 4,
    ctx.canvas.height * 4,
  )
  ctx.restore()
}

function drawBase(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  colorMix: number,
  softness: number,
  random: () => number,
): void {
  const palette = paletteAt(colorMix)
  const angle = random() * Math.PI * 2
  const x1 = width * (0.5 + Math.cos(angle) * 0.5)
  const y1 = height * (0.5 + Math.sin(angle) * 0.5)
  const base = ctx.createLinearGradient(x1, y1, width - x1, height - y1)
  base.addColorStop(0, rgbToCss(palette[0], 1))
  base.addColorStop(0.44, rgbToCss(palette[1], 1))
  base.addColorStop(1, rgbToCss(palette[2], 1))
  ctx.fillStyle = base
  ctx.fillRect(0, 0, width, height)

  const count = Math.round(4 + softness / 12)
  for (let i = 0; i < count; i += 1) {
    const inner = palette[(i + 1) % palette.length]
    const outer = palette[(i + 3) % palette.length]
    addRadial(
      ctx,
      lerp(-0.1, 1.1, random()) * width,
      lerp(-0.1, 1.1, random()) * height,
      lerp(0.38, 0.82, random()) * Math.max(width, height) * (softness / 70),
      inner,
      outer,
      lerp(0.18, 0.52, random()),
    )
  }
}

function drawBrush(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  brush: number,
  preset: DiffusionPreset,
  palette: Rgb[],
  random: () => number,
): void {
  if (brush < 5) return
  ctx.save()
  ctx.globalCompositeOperation = preset === 'watercolor' ? 'multiply' : 'soft-light'
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  const count = Math.round(8 + brush / 4)
  for (let i = 0; i < count; i += 1) {
    const color = palette[i % palette.length]
    ctx.strokeStyle = rgbToCss(color, lerp(0.04, 0.16, random()) * (brush / 70))
    ctx.lineWidth = lerp(width * 0.025, width * 0.12, random())
    ctx.beginPath()
    const startY = lerp(-0.1, 1.1, random()) * height
    ctx.moveTo(-width * 0.12, startY)
    ctx.bezierCurveTo(
      width * lerp(0.15, 0.35, random()),
      height * lerp(-0.2, 1.2, random()),
      width * lerp(0.58, 0.8, random()),
      height * lerp(-0.2, 1.2, random()),
      width * 1.12,
      height * lerp(-0.1, 1.1, random()),
    )
    ctx.stroke()
  }
  ctx.restore()
}

function drawHorizon(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  bands: number,
  palette: Rgb[],
  random: () => number,
): void {
  if (bands < 4) return
  const bottom = height * lerp(0.72, 0.86, random())
  const intensity = bands / 100
  ctx.save()
  ctx.globalCompositeOperation = 'multiply'
  const dark = ctx.createLinearGradient(0, bottom - height * 0.06, width, bottom + height * 0.12)
  dark.addColorStop(0, rgbToCss(palette[4], 0))
  dark.addColorStop(0.52, rgbToCss(palette[4], 0.84 * intensity))
  dark.addColorStop(1, rgbToCss(palette[4], 0.96 * intensity))
  ctx.fillStyle = dark
  ctx.fillRect(0, bottom - height * 0.08, width, height * 0.32)
  ctx.globalCompositeOperation = 'screen'
  for (let i = 0; i < 4; i += 1) {
    const y = bottom - height * (0.03 + i * 0.035)
    const band = ctx.createLinearGradient(0, y, width, y + height * 0.04)
    band.addColorStop(0, rgbToCss(palette[(i + 2) % 5], 0))
    band.addColorStop(0.45, rgbToCss(palette[i % 5], 0.4 * intensity))
    band.addColorStop(1, rgbToCss(palette[(i + 1) % 5], 0.66 * intensity))
    ctx.fillStyle = band
    ctx.filter = `blur(${Math.round(height * 0.018)}px)`
    ctx.fillRect(0, y, width, height * 0.055)
  }
  ctx.restore()
}

function drawAurora(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  s: DiffusionSettings,
  palette: Rgb[],
  random: () => number,
): void {
  const intensity = clamp(
    (s.softness * 0.48 + s.texture * 0.24 + s.materialDepth * 0.28) / 100,
    0.32,
    0.92,
  )
  const horizonY = height * lerp(0.72, 0.82, random())
  ctx.save()

  const air = ctx.createLinearGradient(0, 0, 0, height)
  air.addColorStop(0, 'rgba(255, 255, 255, 0.22)')
  air.addColorStop(0.58, rgbToCss(palette[3], 0.16 * intensity))
  air.addColorStop(1, rgbToCss(palette[4], 0.1 * intensity))
  ctx.globalCompositeOperation = 'screen'
  ctx.fillStyle = air
  ctx.fillRect(0, 0, width, height)

  ctx.filter = `blur(${Math.round(height * 0.032)}px)`
  for (let i = 0; i < 7; i += 1) {
    addEllipticalGlow(
      ctx,
      width * lerp(-0.08, 1.08, random()),
      horizonY + height * lerp(-0.1, 0.13, random()),
      width * lerp(0.38, 0.78, random()),
      height * lerp(0.06, 0.18, random()),
      palette[(i + 1) % palette.length],
      palette[(i + 3) % palette.length],
      lerp(0.16, 0.42, random()) * intensity,
    )
  }

  ctx.filter = `blur(${Math.round(height * 0.014)}px)`
  const horizonGlow = ctx.createLinearGradient(
    0,
    horizonY - height * 0.13,
    width,
    horizonY + height * 0.08,
  )
  horizonGlow.addColorStop(0, rgbToCss(palette[3], 0))
  horizonGlow.addColorStop(0.4, rgbToCss(palette[1], 0.34 * intensity))
  horizonGlow.addColorStop(0.68, rgbToCss(palette[0], 0.52 * intensity))
  horizonGlow.addColorStop(1, rgbToCss(palette[2], 0.18 * intensity))
  ctx.fillStyle = horizonGlow
  ctx.fillRect(0, horizonY - height * 0.18, width, height * 0.3)

  ctx.filter = `blur(${Math.round(height * 0.02)}px)`
  const rim = ctx.createLinearGradient(0, horizonY + height * 0.05, width, horizonY + height * 0.08)
  rim.addColorStop(0, rgbToCss(palette[0], 0.18 * intensity))
  rim.addColorStop(0.42, rgbToCss(palette[1], 0.34 * intensity))
  rim.addColorStop(0.72, rgbToCss(palette[2], 0.42 * intensity))
  rim.addColorStop(1, rgbToCss(palette[4], 0.16 * intensity))
  ctx.fillStyle = rim
  ctx.fillRect(0, horizonY - height * 0.02, width, height * 0.16)

  ctx.globalCompositeOperation = 'multiply'
  ctx.filter = 'none'
  const ground = ctx.createLinearGradient(0, horizonY - height * 0.05, 0, height)
  ground.addColorStop(0, rgbToCss(palette[4], 0))
  ground.addColorStop(0.58, rgbToCss(palette[4], 0.34 * intensity))
  ground.addColorStop(1, rgbToCss(palette[4], 0.72 * intensity))
  ctx.fillStyle = ground
  ctx.fillRect(0, horizonY - height * 0.06, width, height * 0.36)

  ctx.globalCompositeOperation = 'screen'
  ctx.filter = `blur(${Math.round(height * 0.018)}px)`
  const finalRim = ctx.createLinearGradient(
    0,
    horizonY + height * 0.02,
    width,
    horizonY + height * 0.04,
  )
  finalRim.addColorStop(0, rgbToCss(palette[0], 0.1 * intensity))
  finalRim.addColorStop(0.38, rgbToCss(palette[1], 0.28 * intensity))
  finalRim.addColorStop(0.7, rgbToCss(palette[2], 0.36 * intensity))
  finalRim.addColorStop(1, rgbToCss(palette[3], 0.14 * intensity))
  ctx.fillStyle = finalRim
  ctx.fillRect(0, horizonY - height * 0.02, width, height * 0.13)
  ctx.restore()
}

function drawPrism(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  s: DiffusionSettings,
  palette: Rgb[],
  random: () => number,
): void {
  const intensity = clamp(
    (s.softness * 0.52 + s.materialDepth * 0.32 + s.brush * 0.16) / 100,
    0.34,
    0.92,
  )
  ctx.save()
  ctx.globalCompositeOperation = 'screen'
  ctx.filter = `blur(${Math.round(width * 0.018)}px)`

  for (let i = 0; i < 6; i += 1) {
    ctx.save()
    ctx.translate(width * 0.5, height * 0.5)
    ctx.rotate(lerp(-0.72, 0.72, random()))
    const y = height * lerp(-0.62, 0.44, random())
    const beam = ctx.createLinearGradient(-width, y, width, y + height * 0.22)
    beam.addColorStop(0, rgbToCss(palette[(i + 2) % palette.length], 0))
    beam.addColorStop(
      0.38,
      rgbToCss(palette[i % palette.length], lerp(0.18, 0.42, random()) * intensity),
    )
    beam.addColorStop(0.62, 'rgba(255, 255, 255, 0.16)')
    beam.addColorStop(1, rgbToCss(palette[(i + 1) % palette.length], 0))
    ctx.fillStyle = beam
    ctx.fillRect(-width * 1.2, y, width * 2.4, height * lerp(0.16, 0.34, random()))
    ctx.restore()
  }

  ctx.filter = `blur(${Math.round(width * 0.04)}px)`
  for (let i = 0; i < 5; i += 1) {
    addEllipticalGlow(
      ctx,
      width * lerp(-0.08, 1.08, random()),
      height * lerp(0.04, 0.96, random()),
      width * lerp(0.24, 0.52, random()),
      height * lerp(0.2, 0.48, random()),
      palette[(i + 3) % palette.length],
      palette[(i + 1) % palette.length],
      lerp(0.12, 0.28, random()) * intensity,
    )
  }

  ctx.globalCompositeOperation = 'soft-light'
  ctx.filter = 'none'
  const veil = ctx.createLinearGradient(0, 0, width, height)
  veil.addColorStop(0, rgbToCss(palette[4], 0.14 * intensity))
  veil.addColorStop(0.5, 'rgba(255, 255, 255, 0.1)')
  veil.addColorStop(1, rgbToCss(palette[2], 0.16 * intensity))
  ctx.fillStyle = veil
  ctx.fillRect(0, 0, width, height)
  ctx.restore()
}

function drawMaterial(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  materialDepth: number,
  palette: Rgb[],
  random: () => number,
): void {
  if (materialDepth < 5) return
  const depth = materialDepth / 100
  ctx.save()
  ctx.globalCompositeOperation = 'screen'
  for (let i = 0; i < 5; i += 1) {
    const y = height * lerp(-0.2, 1.1, random())
    const x = width * lerp(-0.2, 0.8, random())
    const gradient = ctx.createLinearGradient(x, y, x + width * 0.7, y + height * 0.35)
    gradient.addColorStop(0, rgbToCss(palette[(i + 3) % 5], 0))
    gradient.addColorStop(0.4, rgbToCss(palette[(i + 1) % 5], 0.22 * depth))
    gradient.addColorStop(0.55, 'rgba(255, 255, 255, 0.30)')
    gradient.addColorStop(1, rgbToCss(palette[i % 5], 0))
    ctx.fillStyle = gradient
    ctx.filter = `blur(${Math.round(18 + depth * 42)}px)`
    ctx.translate(width * 0.5, height * 0.5)
    ctx.rotate(lerp(-0.75, 0.75, random()))
    ctx.translate(-width * 0.5, -height * 0.5)
    ctx.fillRect(-width * 0.1, y, width * 1.2, height * lerp(0.12, 0.28, random()))
    ctx.setTransform(1, 0, 0, 1, 0, 0)
  }
  ctx.restore()
}

function drawParticles(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  preset: DiffusionPreset,
  materialDepth: number,
  palette: Rgb[],
  random: () => number,
): void {
  if (preset !== 'material' && materialDepth < 70) return
  ctx.save()
  ctx.globalCompositeOperation = 'screen'
  const count = 120 + Math.round(materialDepth * 2.4)
  for (let i = 0; i < count; i += 1) {
    const x = random() * width
    const y = random() * height
    const radius = lerp(1, 6, random()) * (width / 1400)
    ctx.fillStyle = rgbToCss(palette[(i + 2) % palette.length], lerp(0.08, 0.28, random()))
    ctx.beginPath()
    ctx.arc(x, y, radius, 0, Math.PI * 2)
    ctx.fill()
  }
  ctx.restore()
}

function drawVignette(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  amount: number,
): void {
  const a = amount / 100
  if (a < 0.01) return
  const g = ctx.createRadialGradient(
    width * 0.52,
    height * 0.42,
    0,
    width * 0.52,
    height * 0.42,
    Math.max(width, height) * 0.76,
  )
  g.addColorStop(0, 'rgba(255, 255, 255, 0)')
  g.addColorStop(0.62, 'rgba(255, 255, 255, 0)')
  g.addColorStop(1, `rgba(10, 12, 20, ${0.42 * a})`)
  ctx.fillStyle = g
  ctx.fillRect(0, 0, width, height)
}

function drawTexture(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  texture: number,
  random: () => number,
): void {
  if (texture < 1) return
  const amount = texture / 100
  const image = ctx.getImageData(0, 0, width, height)
  const d = image.data
  for (let i = 0; i < d.length; i += 4) {
    const noise = (random() - 0.5) * 56 * amount
    d[i] = clamp(d[i] + noise, 0, 255)
    d[i + 1] = clamp(d[i + 1] + noise, 0, 255)
    d[i + 2] = clamp(d[i + 2] + noise, 0, 255)
  }
  ctx.putImageData(image, 0, 0)

  if (amount > 0.32) {
    ctx.save()
    ctx.globalCompositeOperation = 'soft-light'
    ctx.strokeStyle = `rgba(255, 255, 255, ${0.035 * amount})`
    ctx.lineWidth = Math.max(1, width / 900)
    for (let y = -height; y < height * 2; y += 9) {
      ctx.beginPath()
      ctx.moveTo(-width * 0.1, y)
      ctx.lineTo(width * 1.1, y + width * 0.18)
      ctx.stroke()
    }
    ctx.restore()
  }
}

function renderTo(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  preset: DiffusionPreset,
  s: DiffusionSettings,
  seed: number,
): void {
  const random = mulberry32(seed)
  const palette = paletteAt(s.colorMix)
  ctx.clearRect(0, 0, width, height)
  ctx.filter = 'none'
  ctx.globalCompositeOperation = 'source-over'

  drawBase(ctx, width, height, s.colorMix, s.softness, random)

  if (preset === 'horizon') {
    drawHorizon(ctx, width, height, s.bands, palette, random)
  } else if (preset === 'aurora') {
    drawAurora(ctx, width, height, s, palette, random)
  } else if (preset === 'prism') {
    drawPrism(ctx, width, height, s, palette, random)
  } else if (preset === 'watercolor') {
    drawBrush(ctx, width, height, Math.max(s.brush, 82), preset, palette, random)
  } else if (preset === 'material') {
    drawMaterial(ctx, width, height, s.materialDepth, palette, random)
    drawParticles(ctx, width, height, preset, s.materialDepth, palette, random)
  } else if (preset === 'mono') {
    ctx.save()
    ctx.globalCompositeOperation = 'color'
    ctx.fillStyle = rgbToCss(palette[2], 0.5)
    ctx.fillRect(0, 0, width, height)
    ctx.restore()
  }

  if (preset !== 'watercolor' && preset !== 'aurora') {
    drawBrush(ctx, width, height, s.brush, preset, palette, random)
  }
  if (preset !== 'horizon') {
    drawHorizon(ctx, width, height, s.bands, palette, random)
  }
  drawMaterial(ctx, width, height, s.materialDepth * 0.45, palette, random)
  drawVignette(ctx, width, height, s.vignette)
  drawTexture(ctx, width, height, s.texture, random)
}

// Output dimensions for a frame at a given base size: square 1:1, wide 16:9, card ~0.563.
export function dimensionsFor(
  frame: DiffusionFrame,
  base: number,
): { width: number; height: number } {
  if (frame === 'wide') return { width: base, height: Math.round((base * 9) / 16) }
  if (frame === 'card') return { width: base, height: Math.round(base * 0.563) }
  return { width: base, height: base }
}

// Hash a string (program_id) into a stable seed, so each program gets a fixed but distinct diffusion base.
export function seedFromString(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) % 900000
}

export function renderDiffusionDataUrl(
  width: number,
  height: number,
  opts: DiffusionOptions = {},
): string {
  const preset = opts.preset ?? 'diffusion'
  const base = PRESET_DEFAULTS[preset]
  const s: DiffusionSettings = {
    colorMix: opts.colorMix ?? base.colorMix,
    softness: opts.softness ?? base.softness,
    texture: opts.texture ?? base.texture,
    materialDepth: opts.materialDepth ?? base.materialDepth,
    bands: opts.bands ?? base.bands,
    brush: opts.brush ?? base.brush,
    vignette: opts.vignette ?? base.vignette,
  }
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  if (!ctx) return ''
  renderTo(ctx, width, height, preset, s, opts.seed ?? 1209)
  return canvas.toDataURL('image/png')
}

// Evolution lab: score a candidate by closeness to a soft, well-mixed diffusion look plus its strongest special trait.
export function scoreCandidate(s: DiffusionSettings): number {
  const diffusionScore = 1 - Math.abs(s.softness - 72) / 100
  const textureScore = 1 - Math.abs(s.texture - 56) / 100
  const colorScore = s.colorMix > 8 && s.colorMix < 92 ? 1 : 0.72
  const specialScore = Math.max(s.materialDepth, s.bands, s.brush) / 100
  return clamp(
    diffusionScore * 0.32 + textureScore * 0.26 + colorScore * 0.18 + specialScore * 0.24,
    0,
    1,
  )
}

function mutateSettings(base: DiffusionSettings, random: () => number): DiffusionSettings {
  const next = { ...base }
  ;(
    ['colorMix', 'softness', 'texture', 'materialDepth', 'bands', 'brush', 'vignette'] as const
  ).forEach((key) => {
    const swing = key === 'colorMix' ? 34 : 26
    next[key] = Math.round(clamp(next[key] + (random() - 0.5) * swing, 0, 100))
  })
  return next
}

export interface DiffusionCandidate {
  settings: DiffusionSettings
  seed: number
  score: number
}

// Deterministically derive 10 mutated candidates from a base setting + seed (mirrors the Atelier evolve()).
export function generateCandidates(
  base: DiffusionSettings,
  baseSeed: number,
): DiffusionCandidate[] {
  const random = mulberry32(baseSeed + 4417)
  const out: DiffusionCandidate[] = []
  for (let i = 0; i < 10; i += 1) {
    const settings = mutateSettings(base, random)
    const seed = baseSeed + 101 + i * 37 + Math.floor(random() * 999)
    out.push({ settings, seed, score: scoreCandidate(settings) })
  }
  return out
}
