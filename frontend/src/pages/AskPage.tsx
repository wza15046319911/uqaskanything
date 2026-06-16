import { useEffect, useRef, useState } from 'react'
import { Alert, Button, InputGroup, Spinner, TextField } from '@heroui/react'
import { motion, useReducedMotion } from 'motion/react'
import Results from '../components/Results'
import { fetchAskStream, type AskMeta, type AskResult } from '../api/ask'
import { easeOut } from '../lib/motion'

const EXAMPLES = [
  '跟机器学习相关、没有考试的课',
  '介绍一下 CSSE1001',
  'CSSE1001是哪些专业的必修',
  'census date 是什么时候',
  '怎么申请缓考',
  'St Lucia 校区停车怎么收费',
]

const PLACEHOLDER = '问点什么…比如:跟机器学习相关、没有考试的课'

interface Turn {
  id: number
  question: string
  meta: AskMeta | null
  answer: string
  retrieving: boolean
  streaming: boolean
  err: string | null
}

interface ExampleChipsProps {
  onPick: (q: string) => void
  className?: string
}

function ExampleChips({ onPick, className = '' }: ExampleChipsProps) {
  return (
    <div className={`flex flex-wrap gap-2.5 ${className}`}>
      {EXAMPLES.map((t) => (
        <Button
          key={t}
          size="sm"
          variant="tertiary"
          className="rounded-full font-normal"
          onPress={() => onPick(t)}
        >
          {t}
        </Button>
      ))}
    </div>
  )
}

interface ComposerProps {
  value: string
  onChange: (v: string) => void
  onSend: () => void
  busy: boolean
}

function Composer({ value, onChange, onSend, busy }: ComposerProps) {
  return (
    <TextField aria-label={PLACEHOLDER} className="w-full">
      <InputGroup className="rounded-full">
        <InputGroup.Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && onSend()}
          placeholder={PLACEHOLDER}
          autoComplete="off"
          enterKeyHint="send"
        />
        <InputGroup.Suffix>
          <Button
            isIconOnly
            isPending={busy}
            onPress={onSend}
            size="sm"
            className="rounded-full"
            aria-label="提问"
          >
            {({ isPending }) =>
              isPending ? (
                <Spinner color="current" size="sm" />
              ) : (
                <svg
                  width="16"
                  height="16"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.4"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M12 19V5M6 11l6-6 6 6" />
                </svg>
              )
            }
          </Button>
        </InputGroup.Suffix>
      </InputGroup>
    </TextField>
  )
}

function Thinking() {
  return (
    <div
      className="flex items-center gap-1.5 py-1.5 text-muted"
      role="status"
      aria-label="正在生成回答"
    >
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-2 w-2 animate-bounce rounded-full bg-current"
          style={{ animationDelay: `${i * 0.15}s` }}
        />
      ))}
    </div>
  )
}

interface ChatTurnProps {
  turn: Turn
}

function ChatTurn({ turn }: ChatTurnProps) {
  const reduce = useReducedMotion()
  const synth: AskResult = {
    mode: turn.meta?.mode,
    answer: turn.answer,
    courses: turn.meta?.courses,
    program_facts: turn.meta?.program_facts,
    chunks: turn.meta?.chunks,
    course: turn.meta?.course,
  }
  return (
    <motion.div
      initial={reduce ? false : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={easeOut}
      className="space-y-4"
    >
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl rounded-br-md bg-accent-soft px-4 py-2.5 text-[15.5px] leading-snug font-medium text-accent-soft-foreground">
          {turn.question}
        </div>
      </div>

      <div className="min-w-0" aria-live="polite">
        {turn.err ? (
          <Alert status="danger">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>出错了</Alert.Title>
              <Alert.Description>{turn.err}</Alert.Description>
            </Alert.Content>
          </Alert>
        ) : turn.retrieving || !turn.meta ? (
          <Thinking />
        ) : (
          <Results res={synth} streaming={turn.streaming} />
        )}
      </div>
    </motion.div>
  )
}

export default function AskPage() {
  const [q, setQ] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const idRef = useRef(0)
  const lastTurnRef = useRef<HTMLDivElement>(null)

  const last = turns[turns.length - 1]
  const busy = !!last?.streaming

  useEffect(() => {
    lastTurnRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  }, [turns.length])

  const ask = async (question?: string) => {
    const text = (question ?? q).trim()
    if (!text || busy) return
    setQ('')
    const id = ++idRef.current
    setTurns((prev) => [
      ...prev,
      { id, question: text, meta: null, answer: '', retrieving: true, streaming: true, err: null },
    ])
    const patch = (p: Partial<Turn>) =>
      setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...p } : t)))
    await fetchAskStream(text, {
      onMeta: (m) => patch({ meta: m, retrieving: false }),
      onToken: (d) =>
        setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, answer: t.answer + d } : t))),
      onDone: (full) => patch({ answer: full, streaming: false, retrieving: false }),
      onError: (msg) => patch({ err: msg, streaming: false, retrieving: false }),
    })
    patch({ streaming: false, retrieving: false })
  }

  const empty = turns.length === 0

  if (empty) {
    return (
      <div className="mx-auto flex min-h-[calc(100dvh-64px)] w-full max-w-2xl flex-col justify-center px-5 pb-24">
        <h1 className="mb-20 text-center text-[clamp(26px,5vw,36px)] leading-tight font-semibold tracking-tight">
          有什么想问 UQ 的?
        </h1>
        <Composer value={q} onChange={setQ} onSend={() => ask()} busy={busy} />
        <ExampleChips onPick={(t) => ask(t)} className="mt-10" />
      </div>
    )
  }

  return (
    <div className="mx-auto flex h-[calc(100dvh-64px)] w-full max-w-3xl flex-col px-5">
      <div className="min-h-0 flex-1 space-y-8 overflow-y-auto py-8 pr-6 [scrollbar-gutter:stable]">
        {turns.map((t, i) => (
          <div key={t.id} ref={i === turns.length - 1 ? lastTurnRef : null}>
            <ChatTurn turn={t} />
          </div>
        ))}
      </div>
      <div className="border-t border-separator bg-background pt-3 pb-5">
        <ExampleChips onPick={(t) => ask(t)} className="mb-3" />
        <Composer value={q} onChange={setQ} onSend={() => ask()} busy={busy} />
      </div>
    </div>
  )
}
