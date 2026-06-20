import {
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ChangeEvent, CSSProperties, Ref } from 'react'
import {
  Button,
  ColorArea,
  ColorField,
  ColorPicker,
  ColorSlider,
  ColorSwatch,
  Input,
  Label,
  ListBox,
  parseColor,
  Select,
  TextArea,
  TextField,
  toast,
} from '@heroui/react'
import { useTranslation } from 'react-i18next'
import { exportNodePng } from '../lib/export-image'
import { dominantHex, renderDiffusionDataUrl, seedFromString } from '../lib/diffusion-bg'
import DiffusionControls, { type DiffusionParams } from '../components/DiffusionControls'
import XhsFeedPreview, { XHS_THUMB_W } from '../components/XhsFeedPreview'
import bgUrl from '../assets/uq-cover-bg.jpg'
import styles from './CoverPage.module.css'

type BgType = 'uqphoto' | 'sketch' | 'diffusion' | 'none' | 'photo'
type CoverType = 'review' | 'combo' | 'content'

const DEFAULT_BASE = '#534AB7'

// Long-text pagination geometry, in the 1080×1440 export canvas. The measuring element below must
// match .contentBody exactly (width / font / spacing / wrap), or the page breaks drift from render.
const CONTENT_W = 900
const CONTENT_MAX_H = 1130
const CONTENT_FONT =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif"
const CONTENT_PREVIEW_SCALE = 0.28

// Split a paragraph into sentences, keeping the trailing punctuation on each piece.
const splitSentences = (s: string) => s.match(/[^。！？!?]+[。！？!?]*/g) ?? [s]

// Greedily pack units into pages: keep appending while it still fits; when a unit overflows on its
// own, fall back to splitOversize to break it down further. fits() measures the candidate string.
const packUnits = (
  units: string[],
  join: string,
  fits: (s: string) => boolean,
  splitOversize: (u: string) => string[],
): string[] => {
  const pages: string[] = []
  let cur = ''
  for (const u of units) {
    const candidate = cur ? cur + join + u : u
    if (fits(candidate)) {
      cur = candidate
      continue
    }
    if (cur) {
      pages.push(cur)
      cur = ''
    }
    if (fits(u)) {
      cur = u
      continue
    }
    const sub = splitOversize(u)
    for (let i = 0; i < sub.length - 1; i++) pages.push(sub[i])
    cur = sub[sub.length - 1] ?? ''
  }
  if (cur) pages.push(cur)
  return pages
}

// Sentence-level pagination: flatten the text into sentence atoms (remembering where a new line/
// paragraph begins so the original line structure survives), then greedily fill each page to the
// brim. Paragraphs only break at a page boundary; a sentence longer than a page falls back to chars.
const paginateText = (text: string, fits: (s: string) => boolean): string[] => {
  const atoms: { text: string; newLine: boolean }[] = []
  text.split('\n').forEach((line, li) => {
    splitSentences(line).forEach((s, si) => atoms.push({ text: s, newLine: li > 0 && si === 0 }))
  })

  const splitChars = (u: string) => packUnits(Array.from(u), '', fits, (c) => [c])
  const pages: string[] = []
  let cur = ''
  for (const a of atoms) {
    if (cur === '' && a.text === '') continue
    const candidate = cur === '' ? a.text : cur + (a.newLine ? '\n' : '') + a.text
    if (fits(candidate)) {
      cur = candidate
      continue
    }
    if (cur) {
      pages.push(cur)
      cur = ''
    }
    if (a.text === '') continue
    if (fits(a.text)) {
      cur = a.text
      continue
    }
    const sub = splitChars(a.text)
    for (let i = 0; i < sub.length - 1; i++) pages.push(sub[i])
    cur = sub[sub.length - 1] ?? ''
  }
  if (cur) pages.push(cur)
  return pages.length ? pages : ['']
}

// Decoration scheme = the base hex that drives the accent / line / chip ramp. 'auto' is special: it
// follows the diffusion background's dominant hue. The rest are fixed presets spanning the bg palettes.
type SchemeId = 'auto' | 'blue' | 'violet' | 'magenta' | 'teal' | 'amber' | 'slate'
const DECO_SCHEMES: { id: SchemeId; base: string }[] = [
  { id: 'auto', base: DEFAULT_BASE },
  { id: 'blue', base: '#2F6FD0' },
  { id: 'violet', base: '#6B55D8' },
  { id: 'magenta', base: '#C2479E' },
  { id: 'teal', base: '#15A8D8' },
  { id: 'amber', base: '#E0902B' },
  { id: 'slate', base: '#5C6675' },
]
const DEFAULT_TEXT = '#29245B'

// Text hierarchy is alpha steps off the picked font color, so the chosen color is used directly
// (white stays white) instead of being flattened into a fixed-lightness ramp like the palette.
const TEXT_ALPHA = { deep: 'ff', mid: 'd9', light: '94', pale: '73' }
const withAlpha = (hex: string, a: string) => `${hex.slice(0, 7)}${a}`

// The card needs a full 7-step shade ramp (deep→card); derive it from one base hex by keeping
// the base hue/saturation and stepping lightness, so any picked color drives the whole layout.
const RAMP_L = { deep: 0.25, mid: 0.37, main: 0.5, light: 0.67, pale: 0.79, line: 0.88, card: 0.96 }

const hexToHsl = (hex: string) => {
  const r = parseInt(hex.slice(1, 3), 16) / 255
  const g = parseInt(hex.slice(3, 5), 16) / 255
  const b = parseInt(hex.slice(5, 7), 16) / 255
  const max = Math.max(r, g, b)
  const min = Math.min(r, g, b)
  const l = (max + min) / 2
  const d = max - min
  let h = 0
  let s = 0
  if (d !== 0) {
    s = d / (1 - Math.abs(2 * l - 1))
    if (max === r) h = ((g - b) / d) % 6
    else if (max === g) h = (b - r) / d + 2
    else h = (r - g) / d + 4
    h *= 60
    if (h < 0) h += 360
  }
  return { h, s, l }
}

const hslToHex = (h: number, s: number, l: number) => {
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1))
  const m = l - c / 2
  let r = 0
  let g = 0
  let b = 0
  if (h < 60) [r, g, b] = [c, x, 0]
  else if (h < 120) [r, g, b] = [x, c, 0]
  else if (h < 180) [r, g, b] = [0, c, x]
  else if (h < 240) [r, g, b] = [0, x, c]
  else if (h < 300) [r, g, b] = [x, 0, c]
  else [r, g, b] = [c, 0, x]
  const to = (v: number) =>
    Math.round((v + m) * 255)
      .toString(16)
      .padStart(2, '0')
  return `#${to(r)}${to(g)}${to(b)}`
}

const deriveRamp = (base: string) => {
  const { h, s } = hexToHsl(base)
  const at = (l: number) => hslToHex(h, s, l)
  return {
    deep: at(RAMP_L.deep),
    mid: at(RAMP_L.mid),
    main: at(RAMP_L.main),
    light: at(RAMP_L.light),
    pale: at(RAMP_L.pale),
    card: at(RAMP_L.card),
    line: at(RAMP_L.line),
  }
}

interface FieldTextProps {
  label: string
  value: string
  onChange: (v: string) => void
  placeholder?: string
  hint?: string
}

function FieldText({ label, value, onChange, placeholder, hint }: FieldTextProps) {
  return (
    <div>
      <TextField value={value} onChange={onChange} className="w-full">
        <Label>{label}</Label>
        <Input placeholder={placeholder} />
      </TextField>
      {hint ? <p className="mt-1 text-xs text-muted">{hint}</p> : null}
    </div>
  )
}

interface SelectOption {
  id: string
  label: string
}

interface FieldSelectProps {
  label: string
  value: string
  options: SelectOption[]
  onChange: (id: string) => void
}

function FieldSelect({ label, value, options, onChange }: FieldSelectProps) {
  return (
    <Select
      selectedKey={value}
      onSelectionChange={(k) => k != null && onChange(String(k))}
      className="w-full"
    >
      <Label>{label}</Label>
      <Select.Trigger>
        <Select.Value />
        <Select.Indicator />
      </Select.Trigger>
      <Select.Popover>
        <ListBox>
          {options.map((o) => (
            <ListBox.Item key={o.id} id={o.id} textValue={o.label}>
              {o.label}
              <ListBox.ItemIndicator />
            </ListBox.Item>
          ))}
        </ListBox>
      </Select.Popover>
    </Select>
  )
}

interface CoverCardProps {
  cardVars: CSSProperties
  showBgImage: boolean
  bgSrc: string | null
  showSketch: boolean
  coverType: CoverType
  code: string
  name: string
  quote: string
  comboTerm: string
  comboSubtitle: string
  cardId: string
  contentText?: string
  pageNum?: number
  pageTotal?: number
  scale?: number
  rootRef?: Ref<HTMLDivElement>
}

function CoverCard({
  cardVars,
  showBgImage,
  bgSrc,
  showSketch,
  coverType,
  code,
  name,
  quote,
  comboTerm,
  comboSubtitle,
  cardId,
  contentText,
  pageNum,
  pageTotal,
  scale,
  rootRef,
}: CoverCardProps) {
  const codeRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLDivElement>(null)
  const quoteCardRef = useRef<HTMLDivElement>(null)

  // Vertical layout: measure the real height of each block, center the whole thing and shift it up a little, so it does not look top-heavy.
  useLayoutEffect(() => {
    const codeEl = codeRef.current
    const nameEl = nameRef.current
    const quoteCardEl = quoteCardRef.current
    if (!codeEl || !nameEl || !quoteCardEl) return

    // Shrink the font size when the course code is too long, to avoid overflowing the right edge
    const codeBase = 160
    const codeMaxW = 900
    codeEl.style.fontSize = `${codeBase}px`
    const codeW = codeEl.offsetWidth
    if (codeW > codeMaxW) {
      codeEl.style.fontSize = `${Math.floor((codeBase * codeMaxW) / codeW)}px`
    }

    const GAP = {
      codeName: 18,
      nameQuote: 130,
    }
    const codeH = codeEl.offsetHeight
    const nameH = nameEl.offsetHeight
    const quoteH = quoteCardEl.offsetHeight

    const total = codeH + GAP.codeName + nameH + GAP.nameQuote + quoteH

    let cy = Math.max(110, (1440 - total) / 2 - 100)
    codeEl.style.top = `${cy}px`
    cy += codeH + GAP.codeName
    nameEl.style.top = `${cy}px`
    cy += nameH + GAP.nameQuote
    quoteCardEl.style.top = `${cy}px`
    // No dependency array: re-layout by the real DOM size after each render (longer text changes the height, which a static dependency cannot express)
  })

  const rootStyle = scale != null ? { ...cardVars, transform: `scale(${scale})` } : cardVars

  return (
    <div ref={rootRef} className={styles.cardRoot} style={rootStyle}>
      {showBgImage && bgSrc ? (
        <img className={styles.bgPhoto} src={bgSrc} alt="" style={{ display: 'block' }} />
      ) : null}
      {showSketch ? (
        <div className={styles.bgSketch}>
          <svg
            viewBox="0 0 1080 700"
            xmlns="http://www.w3.org/2000/svg"
            preserveAspectRatio="xMidYMax meet"
          >
            <g
              fill="none"
              stroke="var(--c-main)"
              strokeWidth="2.4"
              strokeLinejoin="round"
              strokeLinecap="round"
            >
              <line x1="40" y1="650" x2="1040" y2="650" />
              <rect x="120" y="120" width="150" height="530" />
              <rect x="120" y="120" width="150" height="46" />
              <path d="M112 120 L195 58 L278 120 Z" />
              <line x1="195" y1="58" x2="195" y2="32" />
              <circle cx="195" cy="210" r="34" />
              <line x1="195" y1="210" x2="195" y2="188" />
              <line x1="195" y1="210" x2="212" y2="216" />
              <rect x="150" y="300" width="40" height="92" rx="20" />
              <rect x="200" y="300" width="40" height="92" rx="20" />
              <rect x="150" y="430" width="40" height="92" rx="20" />
              <rect x="200" y="430" width="40" height="92" rx="20" />
              <g>
                <rect x="300" y="360" width="700" height="290" />
                <line x1="300" y1="360" x2="1000" y2="360" />
                <path d="M330 650 L330 470 Q330 430 372 430 Q414 430 414 470 L414 650" />
                <path d="M444 650 L444 470 Q444 430 486 430 Q528 430 528 470 L528 650" />
                <path d="M558 650 L558 470 Q558 430 600 430 Q642 430 642 470 L642 650" />
                <path d="M672 650 L672 470 Q672 430 714 430 Q756 430 756 470 L756 650" />
                <path d="M786 650 L786 470 Q786 430 828 430 Q870 430 870 470 L870 650" />
                <path d="M900 650 L900 470 Q900 430 942 430 Q970 430 970 470 L970 650" />
                {Array.from({ length: 15 }, (_, i) => 300 + i * 50).map((x) => (
                  <line key={x} x1={x} y1={360} x2={x} y2={338} />
                ))}
              </g>
            </g>
          </svg>
        </div>
      ) : null}
      <div className={styles.scrim} />
      <div className={styles.frameBorder} />
      {coverType === 'content' ? (
        <div className={styles.contentBody}>{contentText}</div>
      ) : coverType === 'review' ? (
        <>
          <div ref={codeRef} className={styles.code}>
            {code}
          </div>
          <div ref={nameRef} className={styles.cname}>
            {name}
          </div>
          <div ref={quoteCardRef} className={styles.quoteCard}>
            <div className={styles.qtext}>{quote}</div>
          </div>
        </>
      ) : (
        <div className={styles.comboWrap}>
          <div className={styles.comboAccent} />
          <div className={styles.comboTitle}>{comboTerm}</div>
          {comboSubtitle ? <div className={styles.comboSub}>{comboSubtitle}</div> : null}
        </div>
      )}
      {coverType === 'content' ? (
        <div className={styles.footer} style={{ justifyContent: 'space-between' }}>
          {cardId ? <div className={styles.right}>{cardId}</div> : <span />}
          {pageTotal && pageTotal > 1 ? (
            <div className={styles.right}>
              {pageNum} / {pageTotal}
            </div>
          ) : null}
        </div>
      ) : (
        <div className={styles.footer}>
          <div className={styles.right}>{cardId}</div>
        </div>
      )}
    </div>
  )
}

export default function CoverPage() {
  const { t } = useTranslation()
  const coverOptions = [
    { id: 'review', label: t('cover.coverTypeReview') },
    { id: 'combo', label: t('cover.coverTypeCombo') },
    { id: 'content', label: t('cover.coverTypeContent') },
  ]
  const bgOptions = [
    { id: 'uqphoto', label: t('cover.bgUqphoto') },
    { id: 'sketch', label: t('cover.bgSketch') },
    { id: 'diffusion', label: t('cover.bgDiffusion') },
    { id: 'none', label: t('cover.bgNone') },
    { id: 'photo', label: t('cover.bgPhoto') },
  ]

  const [coverType, setCoverType] = useState<CoverType>('review')
  const [code, setCode] = useState('INFS7410')
  const [name, setName] = useState(() => t('cover.default.name'))
  const [quote, setQuote] = useState(() => t('cover.default.quote'))
  const [comboTerm, setComboTerm] = useState(() => t('cover.default.comboTerm'))
  const [comboSubtitle, setComboSubtitle] = useState('BACHELOR OF COMPUTER SCIENCE')
  const [contentText, setContentText] = useState(() => t('cover.default.content'))
  const [contentPages, setContentPages] = useState<string[]>([''])
  const [cardId, setCardId] = useState('@nilobjectfound')
  const [noteTitle, setNoteTitle] = useState(() => t('cover.default.noteTitle'))
  const [decoScheme] = useState<SchemeId>('auto')
  // Keep the picked color as an HSB Color: hex drops hue/saturation at zero brightness/saturation, which froze the hue slider on black. Downstream still reads a hex via textColor.
  const [textColorObj, setTextColorObj] = useState(() => parseColor(DEFAULT_TEXT).toFormat('hsb'))
  const textColor = textColorObj.toString('hex')
  const [bgType, setBgType] = useState<BgType>('diffusion')
  const [diff, setDiff] = useState<DiffusionParams>(() => ({
    preset: 'diffusion',
    frame: 'square',
    colorMix: 18,
    softness: 78,
    texture: 30,
    materialDepth: 28,
    bands: 0,
    brush: 24,
    vignette: 20,
    seed: seedFromString('uq-cover'),
  }))
  const [photo, setPhoto] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [copied, setCopied] = useState(false)

  const stageRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const measureRef = useRef<HTMLDivElement>(null)
  const contentCardRefs = useRef<(HTMLDivElement | null)[]>([])

  // 'auto' follows the diffusion background's dominant hue; for other backgrounds it falls back to the default.
  const autoBase = bgType === 'diffusion' ? dominantHex(diff.colorMix) : DEFAULT_BASE
  const decoBase =
    decoScheme === 'auto'
      ? autoBase
      : (DECO_SCHEMES.find((s) => s.id === decoScheme)?.base ?? DEFAULT_BASE)
  const pal = useMemo(() => deriveRamp(decoBase), [decoBase])
  const cardVars = {
    '--c-deep': withAlpha(textColor, TEXT_ALPHA.deep),
    '--c-mid': withAlpha(textColor, TEXT_ALPHA.mid),
    '--c-light': withAlpha(textColor, TEXT_ALPHA.light),
    '--c-pale': withAlpha(textColor, TEXT_ALPHA.pale),
    '--c-main': pal.main,
    '--c-card': pal.card,
    '--c-line': pal.line,
  } as CSSProperties

  const showSketch = bgType === 'sketch'

  // The diffusion background is heavy (1.55M pixels getImageData + PNG encoding). Drive it with a deferred value so the slider responds at once,
  // skips intermediate values while dragging, and produces the final image once dragging stops.
  const deferredDiff = useDeferredValue(diff)
  const diffusionUrl = useMemo(() => {
    if (bgType !== 'diffusion') return null
    return renderDiffusionDataUrl(1080, 1440, {
      preset: deferredDiff.preset,
      colorMix: deferredDiff.colorMix,
      softness: deferredDiff.softness,
      texture: deferredDiff.texture,
      materialDepth: deferredDiff.materialDepth,
      bands: deferredDiff.bands,
      brush: deferredDiff.brush,
      vignette: deferredDiff.vignette,
      seed: deferredDiff.seed,
    })
  }, [
    bgType,
    deferredDiff.preset,
    deferredDiff.colorMix,
    deferredDiff.softness,
    deferredDiff.texture,
    deferredDiff.materialDepth,
    deferredDiff.bands,
    deferredDiff.brush,
    deferredDiff.vignette,
    deferredDiff.seed,
  ])

  // Re-paginate when the text or type changes. Deferred so typing stays responsive while the
  // measure-and-pack loop runs. Measurement uses the hidden element styled like .contentBody.
  const deferredContent = useDeferredValue(contentText)
  useLayoutEffect(() => {
    if (coverType !== 'content') return
    const el = measureRef.current
    if (!el) return
    const fits = (s: string) => {
      el.textContent = s
      return el.scrollHeight <= CONTENT_MAX_H
    }
    setContentPages(paginateText(deferredContent, fits))
  }, [coverType, deferredContent])

  const bgSrc =
    bgType === 'uqphoto'
      ? bgUrl
      : bgType === 'photo'
        ? photo
        : bgType === 'diffusion'
          ? diffusionUrl
          : null
  const showBgImage =
    bgType === 'uqphoto' ||
    (bgType === 'photo' && !!photo) ||
    (bgType === 'diffusion' && !!diffusionUrl)

  // Scale the preview stage proportionally: fit the 1080-wide card into the stage width
  const fitStage = useCallback(() => {
    const stage = stageRef.current
    const card = cardRef.current
    if (!stage || !card) return
    card.style.transform = `scale(${stage.clientWidth / 1080})`
  }, [])

  useEffect(() => {
    fitStage()
    const stage = stageRef.current
    if (!stage) return
    const ro = new ResizeObserver(() => fitStage())
    ro.observe(stage)
    return () => ro.disconnect()
  }, [fitStage, coverType])

  const handleUpload = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => {
      setPhoto(ev.target?.result as string)
      setBgType('photo')
    }
    reader.readAsDataURL(file)
  }

  const handleExport = async () => {
    if (exporting) return
    setExporting(true)
    try {
      if (coverType === 'content') {
        const base = (noteTitle || 'note').replace(/[\\/:*?"<>|\s]+/g, '_').slice(0, 40)
        const cards = contentCardRefs.current.filter(Boolean) as HTMLDivElement[]
        for (let i = 0; i < cards.length; i++) {
          cards[i].style.transform = 'scale(1)'
          await exportNodePng(cards[i], `${base}-${i + 1}.jpg`, { format: 'jpg', quality: 0.92 })
          cards[i].style.transform = `scale(${CONTENT_PREVIEW_SCALE})`
        }
        return
      }
      const card = cardRef.current
      if (!card) return
      card.style.transform = 'scale(1)'
      const base = coverType === 'combo' ? comboTerm || 'combo' : code || 'course'
      await exportNodePng(card, `${base}-xiaohongshu.jpg`, {
        format: 'jpg',
        quality: 0.92,
      })
    } catch (e) {
      toast(t('cover.exportFail', { msg: e instanceof Error ? e.message : String(e) }))
    } finally {
      fitStage()
      setExporting(false)
    }
  }

  const handleCopy = () => {
    const cfg = {
      coverType,
      code,
      name,
      quote,
      comboTerm,
      comboSubtitle,
      contentText,
      id: cardId,
      noteTitle,
      bgType,
      diffusion: diff,
      decoScheme,
      textColor,
    }
    navigator.clipboard.writeText(JSON.stringify(cfg, null, 2)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  const cardProps = {
    cardVars,
    showBgImage,
    bgSrc,
    showSketch,
    coverType,
    code,
    name,
    quote,
    comboTerm,
    comboSubtitle,
    cardId,
  }

  return (
    <div className="mx-auto w-full max-w-[1760px] px-5 py-8">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-foreground">{t('cover.title')}</h1>
        <p className="mt-1 text-[13px] text-muted">{t('cover.subtitle')}</p>
      </div>

      <div
        ref={measureRef}
        aria-hidden
        style={{
          position: 'absolute',
          left: -99999,
          top: 0,
          visibility: 'hidden',
          pointerEvents: 'none',
          width: CONTENT_W,
          fontFamily: CONTENT_FONT,
          fontSize: 46,
          fontWeight: 600,
          lineHeight: 1.6,
          letterSpacing: '1px',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      />

      <div className="flex flex-col gap-6 xl:flex-row xl:items-start">
        <aside className="rounded-2xl border border-border bg-surface p-6 shadow-surface xl:sticky xl:top-6 xl:w-[320px] xl:shrink-0">
          <div className="space-y-4">
            <FieldSelect
              label={t('cover.bgLabel')}
              value={bgType}
              options={bgOptions}
              onChange={(v) => setBgType(v as BgType)}
            />

            {bgType === 'diffusion' ? (
              <DiffusionControls value={diff} onChange={setDiff} previewAspect={1080 / 1440} />
            ) : null}

            {bgType === 'photo' ? (
              <div>
                <Label>{t('cover.uploadLabel')}</Label>
                <input ref={fileRef} type="file" accept="image/*" hidden onChange={handleUpload} />
                <div className="mt-2">
                  <Button variant="tertiary" size="sm" onPress={() => fileRef.current?.click()}>
                    {photo ? t('cover.photoChosen') : t('cover.photoChoose')}
                  </Button>
                </div>
                <p className="mt-1 text-xs text-muted">{t('cover.photoHint')}</p>
              </div>
            ) : null}
            <div>
              <Label>{t('cover.textColorLabel')}</Label>
              <ColorPicker
                value={textColorObj}
                onChange={(c) => setTextColorObj(c.toFormat('hsb'))}
              >
                <ColorPicker.Trigger className="mt-2 flex w-full items-center gap-2.5 rounded-lg border border-border bg-background px-3 py-2 text-sm">
                  <ColorSwatch className="h-6 w-6 rounded-md" />
                  <span className="font-mono uppercase tracking-wider">{textColor}</span>
                </ColorPicker.Trigger>
                <ColorPicker.Popover>
                  <div className="flex w-56 flex-col gap-3 p-3">
                    <ColorArea colorSpace="hsb" xChannel="saturation" yChannel="brightness">
                      <ColorArea.Thumb />
                    </ColorArea>
                    <ColorSlider channel="hue" colorSpace="hsb">
                      <ColorSlider.Track>
                        <ColorSlider.Thumb />
                      </ColorSlider.Track>
                    </ColorSlider>
                    <ColorField>
                      <ColorField.Group>
                        <ColorField.Prefix>#</ColorField.Prefix>
                        <ColorField.Input />
                      </ColorField.Group>
                    </ColorField>
                  </div>
                </ColorPicker.Popover>
              </ColorPicker>
            </div>
          </div>
        </aside>

        <section className="order-first flex min-w-0 flex-1 flex-wrap items-start justify-center gap-6 xl:order-none xl:sticky xl:top-6">
          {coverType === 'content' ? (
            <div className="flex flex-col items-center gap-3.5">
              <div className="flex flex-wrap justify-center gap-3">
                {contentPages.map((p, i) => (
                  <div
                    key={i}
                    className={styles.stage}
                    style={{
                      width: 1080 * CONTENT_PREVIEW_SCALE,
                      height: 1440 * CONTENT_PREVIEW_SCALE,
                    }}
                  >
                    <CoverCard
                      {...cardProps}
                      contentText={p}
                      pageNum={i + 1}
                      pageTotal={contentPages.length}
                      scale={CONTENT_PREVIEW_SCALE}
                      rootRef={(el) => {
                        contentCardRefs.current[i] = el
                      }}
                    />
                  </div>
                ))}
              </div>
              <p className="max-w-[540px] text-center text-xs text-muted">
                {t('cover.contentPages', { n: contentPages.length })}
              </p>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3.5">
              <div ref={stageRef} className={styles.stage}>
                <CoverCard {...cardProps} rootRef={cardRef} />
              </div>
              <p className="max-w-[540px] text-center text-xs text-muted">
                {t('cover.previewNote')}
              </p>
            </div>
          )}
          <XhsFeedPreview
            title={noteTitle}
            author={cardId}
            cover={
              <CoverCard
                {...cardProps}
                contentText={contentPages[0]}
                pageNum={1}
                pageTotal={contentPages.length}
                scale={XHS_THUMB_W / 1080}
              />
            }
          />
        </section>

        <aside className="rounded-2xl border border-border bg-surface p-6 shadow-surface xl:sticky xl:top-6 xl:w-[340px] xl:shrink-0">
          <div className="space-y-4">
            <FieldSelect
              label={t('cover.coverTypeLabel')}
              value={coverType}
              options={coverOptions}
              onChange={(v) => setCoverType(v as CoverType)}
            />

            {coverType === 'content' ? (
              <div>
                <TextField value={contentText} onChange={setContentText} className="w-full">
                  <Label>{t('cover.contentLabel')}</Label>
                  <TextArea placeholder={t('cover.contentPlaceholder')} rows={12} />
                </TextField>
                <p className="mt-1 text-xs text-muted">{t('cover.contentHint')}</p>
              </div>
            ) : coverType === 'review' ? (
              <>
                <FieldText
                  label={t('cover.codeLabel')}
                  value={code}
                  onChange={setCode}
                  placeholder={t('cover.codePlaceholder')}
                />
                <FieldText
                  label={t('cover.nameLabel')}
                  value={name}
                  onChange={setName}
                  placeholder={t('cover.namePlaceholder')}
                />

                <div>
                  <TextField value={quote} onChange={setQuote} className="w-full">
                    <Label>{t('cover.quoteLabel')}</Label>
                    <TextArea placeholder={t('cover.quotePlaceholder')} rows={4} />
                  </TextField>
                  <p className="mt-1 text-xs text-muted">{t('cover.quoteHint')}</p>
                </div>
              </>
            ) : (
              <>
                <FieldText
                  label={t('cover.comboTermLabel')}
                  value={comboTerm}
                  onChange={setComboTerm}
                  placeholder={t('cover.comboTermPlaceholder')}
                />
                <FieldText
                  label={t('cover.comboSubtitleLabel')}
                  value={comboSubtitle}
                  onChange={setComboSubtitle}
                  placeholder={t('cover.comboSubtitlePlaceholder')}
                />
              </>
            )}

            <FieldText label={t('cover.cardIdLabel')} value={cardId} onChange={setCardId} />

            <FieldText
              label={t('cover.noteTitleLabel')}
              value={noteTitle}
              onChange={setNoteTitle}
              placeholder={t('cover.noteTitlePlaceholder')}
              hint={t('cover.noteTitleHint')}
            />

            <div className="space-y-2.5 pt-1">
              <Button className="w-full" isDisabled={exporting} onPress={handleExport}>
                {exporting ? t('cover.exporting') : t('cover.exportJpg')}
              </Button>
              <Button className="w-full" variant="ghost" onPress={handleCopy}>
                {copied ? t('cover.copied') : t('cover.copyConfig')}
              </Button>
            </div>
          </div>
        </aside>
      </div>
    </div>
  )
}
