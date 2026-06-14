import {
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ChangeEvent, CSSProperties } from 'react'
import {
  Button,
  Input,
  Label,
  ListBox,
  Select,
  Slider,
  TextArea,
  TextField,
  toast,
} from '@heroui/react'
import { exportNodePng } from '../lib/export-image'
import { renderDiffusionDataUrl, seedFromString } from '../lib/diffusion-bg'
import DiffusionControls, { type DiffusionParams } from '../components/DiffusionControls'
import bgUrl from '../assets/uq-cover-bg.jpg'
import styles from './CoverPage.module.css'

const PALETTES = {
  uqpurple: {
    name: 'UQ 紫',
    deep: '#26215C',
    mid: '#3C3489',
    main: '#534AB7',
    light: '#7F77DD',
    pale: '#AFA9EC',
    card: '#EEEDFE',
    line: '#CECBF6',
  },
  blue: {
    name: '科技蓝',
    deep: '#042C53',
    mid: '#0C447C',
    main: '#185FA5',
    light: '#378ADD',
    pale: '#85B7EB',
    card: '#E6F1FB',
    line: '#B5D4F4',
  },
  teal: {
    name: '清新青',
    deep: '#04342C',
    mid: '#085041',
    main: '#0F6E56',
    light: '#1D9E75',
    pale: '#5DCAA5',
    card: '#E1F5EE',
    line: '#9FE1CB',
  },
  coral: {
    name: '暖珊瑚',
    deep: '#4A1B0C',
    mid: '#712B13',
    main: '#993C1D',
    light: '#D85A30',
    pale: '#F0997B',
    card: '#FAECE7',
    line: '#F5C4B3',
  },
  pink: {
    name: '莓粉',
    deep: '#4B1528',
    mid: '#72243E',
    main: '#993556',
    light: '#D4537E',
    pale: '#ED93B1',
    card: '#FBEAF0',
    line: '#F4C0D1',
  },
  green: {
    name: '鲜绿',
    deep: '#173404',
    mid: '#27500A',
    main: '#3B6D11',
    light: '#639922',
    pale: '#97C459',
    card: '#EAF3DE',
    line: '#C0DD97',
  },
  amber: {
    name: '琥珀',
    deep: '#412402',
    mid: '#633806',
    main: '#854F0B',
    light: '#BA7517',
    pale: '#EF9F27',
    card: '#FAEEDA',
    line: '#FAC775',
  },
  gray: {
    name: '极简灰',
    deep: '#2C2C2A',
    mid: '#444441',
    main: '#5F5E5A',
    light: '#888780',
    pale: '#B4B2A9',
    card: '#F1EFE8',
    line: '#D3D1C7',
  },
} as const

type PaletteKey = keyof typeof PALETTES
type BgType = 'uqphoto' | 'sketch' | 'diffusion' | 'none' | 'photo'
type CoverType = 'review' | 'combo'

const COVER_OPTIONS = [
  { id: 'review', label: '课程攻略（单课程点评）' },
  { id: 'combo', label: '选课搭配（一学期组合）' },
]

const STAR_OPTIONS = [
  { id: '0', label: '不显示' },
  { id: '1', label: '★' },
  { id: '2', label: '★★' },
  { id: '3', label: '★★★' },
  { id: '4', label: '★★★★' },
  { id: '5', label: '★★★★★' },
]

const BG_OPTIONS = [
  { id: 'uqphoto', label: 'UQ Forgan Smith 钟楼（内置）' },
  { id: 'sketch', label: '砂岩建筑线稿' },
  { id: 'diffusion', label: '弥散渐变（生成）' },
  { id: 'none', label: '纯白' },
  { id: 'photo', label: '我自己的照片' },
]

const stars = (n: number) => '★'.repeat(n) + '☆'.repeat(5 - n)

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

export default function CoverPage() {
  const [coverType, setCoverType] = useState<CoverType>('review')
  const [eyebrow, setEyebrow] = useState('UQ选课笔记')
  const [code, setCode] = useState('INFS7410')
  const [name, setName] = useState('信息检索')
  const [quote, setQuote] = useState(
    '难度中等，但逼你绕开 AI\n真正搞懂检索和 RAG 原理\n想入门 IR 的可以闭眼冲 ✅',
  )
  const [difficulty, setDifficulty] = useState(3)
  const [recommend, setRecommend] = useState(5)
  const [comboTerm, setComboTerm] = useState('2026 S2 选课搭配')
  const [comboSubtitle, setComboSubtitle] = useState('BACHELOR OF COMPUTER SCIENCE')
  const [comboCourses, setComboCourses] = useState(
    'INFS2200 数据库系统\nDECO2500 人机交互\nCSSE2310 C 与 Unix 编程\nCOMP3506 数据结构与算法',
  )
  const [comboNote, setComboNote] = useState('大二上的稳健搭配\n两门硬课配一门项目课，强度刚好')
  const [cardId, setCardId] = useState('@nilobjectfound')
  const [tags, setTags] = useState('')
  const [palette, setPalette] = useState<PaletteKey>('uqpurple')
  const [bgType, setBgType] = useState<BgType>('diffusion')
  const [diff, setDiff] = useState<DiffusionParams>(() => ({
    colorMix: 18,
    softness: 78,
    texture: 30,
    seed: seedFromString('uq-cover'),
  }))
  const [fade, setFade] = useState(90)
  const [photo, setPhoto] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [copied, setCopied] = useState(false)

  const stageRef = useRef<HTMLDivElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const accentRef = useRef<HTMLDivElement>(null)
  const eyebrowRef = useRef<HTMLDivElement>(null)
  const codeRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLDivElement>(null)
  const dividerRef = useRef<HTMLDivElement>(null)
  const ratingsRef = useRef<HTMLDivElement>(null)
  const quoteCardRef = useRef<HTMLDivElement>(null)
  const tagsRef = useRef<HTMLDivElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const pal = PALETTES[palette]
  const cardVars = {
    '--c-deep': pal.deep,
    '--c-mid': pal.mid,
    '--c-main': pal.main,
    '--c-light': pal.light,
    '--c-pale': pal.pale,
    '--c-card': pal.card,
    '--c-line': pal.line,
  } as CSSProperties

  const tagTokens = tags.split(/\s+/).filter(Boolean)
  const showTags = tagTokens.length > 0
  const showRatings = difficulty > 0 || recommend > 0
  const showSketch = bgType === 'sketch'

  const courseRows = comboCourses
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const m = line.match(/^(\S+)\s+([\s\S]+)$/)
      return m ? { code: m[1], name: m[2] } : { code: line, name: '' }
    })

  // 弥散底图很重(155 万像素 getImageData + PNG 编码)。用 deferred 值驱动,让滑块即时响应、
  // 拖动时跳过中间值,停下再出最终那张。
  const deferredDiff = useDeferredValue(diff)
  const diffusionUrl = useMemo(() => {
    if (bgType !== 'diffusion') return null
    return renderDiffusionDataUrl(1080, 1440, {
      colorMix: deferredDiff.colorMix,
      softness: deferredDiff.softness,
      texture: deferredDiff.texture,
      seed: deferredDiff.seed,
    })
  }, [
    bgType,
    deferredDiff.colorMix,
    deferredDiff.softness,
    deferredDiff.texture,
    deferredDiff.seed,
  ])

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

  // 弥散底图在白色蒙版下需更高不透明度才显色;切到/离开弥散时把淡化值带到合适区间。
  const handleBgChange = (v: BgType) => {
    setBgType(v)
    if (v === 'diffusion') setFade((f) => (f <= 45 ? 90 : f))
    else setFade((f) => (f > 45 ? 45 : f))
  }
  const fadeMax = bgType === 'diffusion' ? 100 : 45

  // 竖向排版:量出各块真实高度,整体居中并略微上移,避免看起来头重脚轻。
  useLayoutEffect(() => {
    const accent = accentRef.current
    const eyebrowEl = eyebrowRef.current
    const codeEl = codeRef.current
    const nameEl = nameRef.current
    const divider = dividerRef.current
    const ratings = ratingsRef.current
    const quoteCardEl = quoteCardRef.current
    const tagsEl = tagsRef.current
    if (!accent || !eyebrowEl || !codeEl || !nameEl || !divider || !quoteCardEl) return

    // 课程代码太长时缩小字号,避免溢出右边界
    const codeBase = 160
    const codeMaxW = 900
    codeEl.style.fontSize = `${codeBase}px`
    const codeW = codeEl.offsetWidth
    if (codeW > codeMaxW) {
      codeEl.style.fontSize = `${Math.floor((codeBase * codeMaxW) / codeW)}px`
    }

    const GAP = {
      accentEb: 38,
      ebCode: 28,
      codeName: 18,
      nameDiv: 34,
      divRate: 40,
      rateQuote: 96,
      quoteTags: 54,
    }
    const Haccent = 10
    const Hdivider = 3
    const ebH = eyebrowEl.offsetHeight
    const codeH = codeEl.offsetHeight
    const nameH = nameEl.offsetHeight
    const rateH = showRatings && ratings ? ratings.offsetHeight : 0
    const quoteH = quoteCardEl.offsetHeight
    const tagsH = showTags && tagsEl ? tagsEl.offsetHeight : 0

    let total =
      Haccent +
      GAP.accentEb +
      ebH +
      GAP.ebCode +
      codeH +
      GAP.codeName +
      nameH +
      GAP.nameDiv +
      Hdivider
    if (showRatings) total += GAP.divRate + rateH
    total += GAP.rateQuote + quoteH
    if (showTags) total += GAP.quoteTags + tagsH

    let cy = Math.max(110, (1440 - total) / 2 - 100)
    accent.style.top = `${cy}px`
    cy += Haccent + GAP.accentEb
    eyebrowEl.style.top = `${cy}px`
    cy += ebH + GAP.ebCode
    codeEl.style.top = `${cy}px`
    cy += codeH + GAP.codeName
    nameEl.style.top = `${cy}px`
    cy += nameH + GAP.nameDiv
    divider.style.top = `${cy}px`
    cy += Hdivider
    if (showRatings && ratings) {
      cy += GAP.divRate
      ratings.style.top = `${cy}px`
      cy += rateH
    }
    cy += GAP.rateQuote
    quoteCardEl.style.top = `${cy}px`
    cy += quoteH
    if (showTags && tagsEl) {
      cy += GAP.quoteTags
      tagsEl.style.top = `${cy}px`
    }
    // 无依赖数组:每次渲染后按真实 DOM 尺寸重排(文字变长会改变高度,静态依赖无法表达)
  })

  // 预览舞台等比缩放:把 1080 宽的卡片缩进舞台宽度
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
  }, [fitStage])

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
    const card = cardRef.current
    if (!card || exporting) return
    setExporting(true)
    card.style.transform = 'scale(1)'
    try {
      const base = coverType === 'combo' ? comboTerm || 'combo' : code || 'course'
      await exportNodePng(card, `${base}-xiaohongshu.jpg`, {
        format: 'jpg',
        quality: 0.92,
      })
    } catch (e) {
      toast(`导出失败：${e instanceof Error ? e.message : String(e)}`)
    } finally {
      fitStage()
      setExporting(false)
    }
  }

  const handleCopy = () => {
    const cfg = {
      coverType,
      eyebrow,
      code,
      name,
      quote,
      difficulty,
      recommend,
      comboTerm,
      comboSubtitle,
      comboCourses,
      comboNote,
      id: cardId,
      tags,
      bgType,
      diffusion: diff,
      fade,
      palette,
    }
    navigator.clipboard.writeText(JSON.stringify(cfg, null, 2)).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    })
  }

  return (
    <div className="mx-auto w-full max-w-[1400px] px-5 py-8">
      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <aside className="rounded-2xl border border-border bg-surface p-6 shadow-surface lg:sticky lg:top-6 lg:w-[360px] lg:shrink-0">
          <div className="mb-5">
            <h1 className="text-lg font-semibold text-foreground">UQ 选课封面生成器</h1>
            <p className="mt-1 text-[13px] text-muted">
              课程攻略 / 选课搭配 → 实时预览 → 导出小红书竖版图（1080×1440）
            </p>
          </div>

          <div className="space-y-4">
            <FieldSelect
              label="封面类型"
              value={coverType}
              options={COVER_OPTIONS}
              onChange={(v) => setCoverType(v as CoverType)}
            />

            <FieldText label="顶部标签" value={eyebrow} onChange={setEyebrow} />

            {coverType === 'review' ? (
              <>
                <FieldText
                  label="课程代码"
                  value={code}
                  onChange={setCode}
                  placeholder="如 INFS7410"
                />
                <FieldText
                  label="课程名称"
                  value={name}
                  onChange={setName}
                  placeholder="如 信息检索"
                />

                <div>
                  <TextField value={quote} onChange={setQuote} className="w-full">
                    <Label>一句话点评</Label>
                    <TextArea placeholder="支持换行" rows={4} />
                  </TextField>
                  <p className="mt-1 text-xs text-muted">建议 2–3 行，每行别太长</p>
                </div>

                <div className="flex gap-3">
                  <div className="flex-1">
                    <FieldSelect
                      label="难度"
                      value={String(difficulty)}
                      options={STAR_OPTIONS}
                      onChange={(v) => setDifficulty(Number(v))}
                    />
                  </div>
                  <div className="flex-1">
                    <FieldSelect
                      label="推荐"
                      value={String(recommend)}
                      options={STAR_OPTIONS}
                      onChange={(v) => setRecommend(Number(v))}
                    />
                  </div>
                </div>
              </>
            ) : (
              <>
                <FieldText
                  label="大标题"
                  value={comboTerm}
                  onChange={setComboTerm}
                  placeholder="如 2026 S2 选课搭配"
                />
                <FieldText
                  label="英文副标题"
                  value={comboSubtitle}
                  onChange={setComboSubtitle}
                  placeholder="如 BACHELOR OF COMPUTER SCIENCE"
                />
                <div>
                  <TextField value={comboCourses} onChange={setComboCourses} className="w-full">
                    <Label>课程清单</Label>
                    <TextArea placeholder="每行一门：代码 + 空格 + 名称" rows={5} />
                  </TextField>
                  <p className="mt-1 text-xs text-muted">
                    每行一门，如「INFS2200 数据库系统」，建议 3–5 门
                  </p>
                </div>
                <div>
                  <TextField value={comboNote} onChange={setComboNote} className="w-full">
                    <Label>底部说明（可选）</Label>
                    <TextArea placeholder="支持换行" rows={2} />
                  </TextField>
                </div>
              </>
            )}

            <FieldText label="右下角 ID" value={cardId} onChange={setCardId} />
            <FieldText
              label="底部关键词标签（空格分隔）"
              value={tags}
              onChange={setTags}
              hint="3–4 个最佳，带不带 # 都行"
            />

            <div>
              <Label>配色（同骨架换色，方便区分学科）</Label>
              <div className="mt-2 flex flex-wrap gap-2">
                {(Object.keys(PALETTES) as PaletteKey[]).map((key) => (
                  <button
                    key={key}
                    type="button"
                    title={PALETTES[key].name}
                    aria-label={PALETTES[key].name}
                    onClick={() => setPalette(key)}
                    className={`h-7 w-7 rounded-lg border-2 transition-transform hover:scale-110 ${
                      palette === key ? 'border-foreground' : 'border-transparent'
                    }`}
                    style={{ background: PALETTES[key].main }}
                  />
                ))}
              </div>
            </div>

            <FieldSelect
              label="背景"
              value={bgType}
              options={BG_OPTIONS}
              onChange={(v) => handleBgChange(v as BgType)}
            />

            {bgType === 'diffusion' ? <DiffusionControls value={diff} onChange={setDiff} /> : null}

            {bgType === 'photo' ? (
              <div>
                <Label>上传背景照片</Label>
                <input ref={fileRef} type="file" accept="image/*" hidden onChange={handleUpload} />
                <div className="mt-2">
                  <Button variant="tertiary" size="sm" onPress={() => fileRef.current?.click()}>
                    {photo ? '已选择，点击更换' : '选择图片'}
                  </Button>
                </div>
                <p className="mt-1 text-xs text-muted">
                  建议用你自己拍的 UQ 照片（竖图最佳）。仅在本机处理，不会上传到任何服务器。
                </p>
              </div>
            ) : null}

            <Slider
              value={fade}
              onChange={(v) => setFade(Array.isArray(v) ? v[0] : v)}
              minValue={3}
              maxValue={fadeMax}
              step={1}
              className="w-full"
            >
              <div className="mb-1.5 flex items-center justify-between">
                <Label>背景淡化浓度</Label>
                <span className="text-xs text-muted">{fade}%</span>
              </div>
              <Slider.Track>
                <Slider.Fill />
                <Slider.Thumb />
              </Slider.Track>
            </Slider>

            <div className="space-y-2.5 pt-1">
              <Button className="w-full" isDisabled={exporting} onPress={handleExport}>
                {exporting ? '导出中…' : '导出 JPG（2× 高清 · 约 0.5MB）'}
              </Button>
              <Button className="w-full" variant="ghost" onPress={handleCopy}>
                {copied ? '已复制 ✓' : '复制当前配置'}
              </Button>
            </div>
          </div>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col items-center gap-3.5">
          <div ref={stageRef} className={styles.stage}>
            <div ref={cardRef} className={styles.cardRoot} style={cardVars}>
              {showBgImage && bgSrc ? (
                <img
                  className={styles.bgPhoto}
                  src={bgSrc}
                  alt=""
                  style={{ display: 'block', opacity: fade / 100 }}
                />
              ) : null}
              {showSketch ? (
                <div className={styles.bgSketch} style={{ opacity: fade / 100 }}>
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
              {coverType === 'review' ? (
                <>
                  <div ref={accentRef} className={styles.accentBar} />
                  <div ref={eyebrowRef} className={styles.eyebrow}>
                    {eyebrow}
                  </div>
                  <div ref={codeRef} className={styles.code}>
                    {code}
                  </div>
                  <div ref={nameRef} className={styles.cname}>
                    {name}
                  </div>
                  <div ref={dividerRef} className={styles.divider} />
                  <div
                    ref={ratingsRef}
                    className={styles.ratings}
                    style={{ display: showRatings ? 'flex' : 'none' }}
                  >
                    {difficulty > 0 ? (
                      <div>
                        <div className={styles.rlabel}>难度</div>
                        <div className={styles.rstars}>{stars(difficulty)}</div>
                      </div>
                    ) : null}
                    {recommend > 0 ? (
                      <div>
                        <div className={styles.rlabel}>推荐</div>
                        <div className={styles.rstars}>{stars(recommend)}</div>
                      </div>
                    ) : null}
                  </div>
                  <div ref={quoteCardRef} className={styles.quoteCard}>
                    <div className={styles.qtext}>{quote}</div>
                  </div>
                  <div
                    ref={tagsRef}
                    className={styles.tags}
                    style={{ display: showTags ? 'flex' : 'none' }}
                  >
                    {tagTokens.map((t, i) => (
                      <div key={i} className={styles.chip}>
                        {t}
                      </div>
                    ))}
                  </div>
                </>
              ) : (
                <div className={styles.comboWrap}>
                  <div className={styles.comboAccent} />
                  {eyebrow ? <div className={styles.comboEyebrow}>{eyebrow}</div> : null}
                  <div className={styles.comboTitle}>{comboTerm}</div>
                  {comboSubtitle ? <div className={styles.comboSub}>{comboSubtitle}</div> : null}
                  <div className={styles.comboDivider} />
                  <div className={styles.comboList}>
                    {courseRows.map((c, i) => (
                      <div key={i} className={styles.comboRow}>
                        <span className={styles.cidx}>{String(i + 1).padStart(2, '0')}</span>
                        <span className={styles.ccode}>{c.code}</span>
                        <span className={styles.cname2}>{c.name}</span>
                      </div>
                    ))}
                  </div>
                  {comboNote ? <div className={styles.comboNote}>{comboNote}</div> : null}
                  {showTags ? (
                    <div className={styles.comboTags}>
                      {tagTokens.map((t, i) => (
                        <div key={i} className={styles.chip}>
                          {t}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              )}
              <div className={styles.footer}>
                <div className={styles.right}>{cardId}</div>
              </div>
            </div>
          </div>
          <p className="max-w-[540px] text-center text-xs text-muted">
            预览是等比缩小图，导出的是 2160×2880 高清 JPG（约 0.5MB）。换色、改字都会实时更新。
          </p>
        </section>
      </div>
    </div>
  )
}
