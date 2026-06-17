// OpenAI-style "diffusion" gradient background generator: a full port of the diffusion preset from the local tool OpenAI Gradient Atelier.
// The render pipeline maps one-to-one to the original, with the same random-number consumption order: drawBase (linear base + radial soft glow) -> drawBrush (brush strokes)
// -> drawHorizon (horizon bands, skipped directly when bands<4) -> drawMaterial (material glow, intensity x0.45)
// -> drawVignette (vignette) -> drawTexture (grain noise + diagonal weave lines at high texture). Outputs a PNG dataURL.
// Use: laid under the sim/cover export image. Deterministic (fixed seed) -> preview and download match.

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

export interface DiffusionOptions {
  colorMix?: number // 0-100, interpolate the color between palettes
  softness?: number // glow spread
  texture?: number // grain intensity 0-100
  materialDepth?: number // material glow intensity 0-100 (multiplied by 0.45 at render time)
  bands?: number // horizon band intensity 0-100 (<4 not drawn)
  brush?: number // brush intensity 0-100
  vignette?: number // vignette 0-100
  seed?: number
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
  palette: Rgb[],
  random: () => number,
): void {
  if (brush < 5) return
  ctx.save()
  ctx.globalCompositeOperation = 'soft-light'
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
  const {
    colorMix = 12,
    softness = 78,
    texture = 30,
    materialDepth = 28,
    bands = 0,
    brush = 24,
    vignette = 20,
    seed = 1209,
  } = opts
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  if (!ctx) return ''
  const random = mulberry32(seed)
  const palette = paletteAt(colorMix)
  ctx.clearRect(0, 0, width, height)
  ctx.filter = 'none'
  ctx.globalCompositeOperation = 'source-over'
  drawBase(ctx, width, height, colorMix, softness, random)
  drawBrush(ctx, width, height, brush, palette, random)
  drawHorizon(ctx, width, height, bands, palette, random)
  drawMaterial(ctx, width, height, materialDepth * 0.45, palette, random)
  drawVignette(ctx, width, height, vignette)
  drawTexture(ctx, width, height, texture, random)
  return canvas.toDataURL('image/png')
}
