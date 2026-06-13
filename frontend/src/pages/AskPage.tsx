import { useState } from 'react'
import { Alert, Button, InputGroup, Skeleton, Spinner, TextField } from '@heroui/react'
import { motion, useReducedMotion } from 'motion/react'
import Results from '../components/Results'
import { fetchAskStream, type AskMeta, type AskResult } from '../api/ask'
import { easeOut, layoutEase, riseIn } from '../lib/motion'

const EXAMPLES = [
  '跟机器学习相关、没有考试的课',
  '介绍一下 CSSE1001',
  'CSSE1001是哪些专业的必修',
  'census date 是什么时候',
  '怎么申请缓考',
  'St Lucia 校区停车怎么收费',
]

const PLACEHOLDER = '比如:跟机器学习相关、没有考试的课'

export default function AskPage() {
  const [q, setQ] = useState('')
  const [asked, setAsked] = useState(false)
  const [retrieving, setRetrieving] = useState(false)
  const [streaming, setStreaming] = useState(false)
  const [meta, setMeta] = useState<AskMeta | null>(null)
  const [answer, setAnswer] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const reduce = useReducedMotion()

  const rise = (delay: number) =>
    reduce
      ? {}
      : {
          initial: 'hidden' as const,
          animate: 'show' as const,
          variants: riseIn,
          transition: { ...easeOut, delay },
        }

  const ask = async (question?: string) => {
    const text = (question ?? q).trim()
    if (!text || streaming) return
    if (question) setQ(question)
    setAsked(true)
    setErr(null)
    setMeta(null)
    setAnswer('')
    setRetrieving(true)
    setStreaming(true)
    await fetchAskStream(text, {
      onMeta: (m) => {
        setMeta(m)
        setRetrieving(false)
      },
      onToken: (d) => setAnswer((prev) => prev + d),
      onDone: (full) => {
        setAnswer(full)
        setStreaming(false)
        setRetrieving(false)
      },
      onError: (msg) => {
        setErr(msg)
        setStreaming(false)
        setRetrieving(false)
      },
    })
    setStreaming(false)
    setRetrieving(false)
  }

  const synth: AskResult = {
    mode: meta?.mode,
    answer,
    courses: meta?.courses,
    program_facts: meta?.program_facts,
    chunks: meta?.chunks,
    course: meta?.course,
  }

  return (
    <motion.div
      layout={!reduce}
      transition={layoutEase}
      className={`mx-auto w-full px-5 ${asked ? 'max-w-[1600px] pt-8 pb-16' : 'flex min-h-[calc(100dvh-72px)] max-w-xl flex-col justify-center pb-16'}`}
    >
      <motion.div
        layout={!reduce}
        transition={layoutEase}
        className={`grid items-start gap-8 ${asked ? 'lg:grid-cols-2' : 'grid-cols-1'}`}
      >
        {/* 左栏 / Hero —— 内容不变,问后桌面端左对齐并垂直居中 */}
        <motion.div
          layout={!reduce}
          transition={layoutEase}
          className={
            asked
              ? 'mx-auto w-full max-w-sm lg:sticky lg:top-0 lg:flex lg:min-h-[calc(100dvh-64px)] lg:flex-col lg:justify-center'
              : ''
          }
        >
          <header className={asked ? 'text-center lg:text-left' : 'text-center'}>
            <motion.h1
              {...rise(0.05)}
              className="mb-3 text-[clamp(32px,6vw,48px)] leading-[1.05] font-semibold tracking-tight"
            >
              Ask UQ <em className="text-accent not-italic">Anything</em>
            </motion.h1>
            <motion.div
              {...rise(0.15)}
              className="mt-[clamp(20px,4vw,32px)] flex items-center gap-4"
            >
              <TextField aria-label={PLACEHOLDER} className="flex-1">
                <InputGroup>
                  <InputGroup.Prefix>
                    <svg
                      width="16"
                      height="16"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      aria-hidden="true"
                    >
                      <circle cx="11" cy="11" r="7" />
                      <path d="m21 21-4.3-4.3" />
                    </svg>
                  </InputGroup.Prefix>
                  <InputGroup.Input
                    value={q}
                    onChange={(e) => setQ(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && ask()}
                    placeholder={PLACEHOLDER}
                    autoComplete="off"
                    enterKeyHint="search"
                  />
                </InputGroup>
              </TextField>
              <Button isPending={streaming} onPress={() => ask()}>
                {({ isPending }) =>
                  isPending ? (
                    <Spinner color="current" size="sm" />
                  ) : (
                    <>
                      提问
                      <svg
                        width="14"
                        height="14"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2.2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        aria-hidden="true"
                      >
                        <path d="M5 12h14M13 6l6 6-6 6" />
                      </svg>
                    </>
                  )
                }
              </Button>
            </motion.div>
            <motion.div
              {...rise(0.2)}
              className={`mt-5 flex flex-wrap gap-3 ${asked ? 'justify-center lg:justify-start' : 'justify-center'}`}
            >
              {EXAMPLES.map((t) => (
                <Button
                  key={t}
                  size="sm"
                  variant="tertiary"
                  className="rounded-full font-normal"
                  onPress={() => ask(t)}
                >
                  {t}
                </Button>
              ))}
            </motion.div>
          </header>
        </motion.div>

        {/* 右栏 / 结果 —— 问后出现:骨架 -> 流式答案 */}
        {asked && (
          <motion.section
            layout={!reduce}
            initial={reduce ? false : { opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={easeOut}
            className="mx-auto w-full max-w-2xl min-w-0"
            aria-live="polite"
          >
            {err ? (
              <Alert status="danger">
                <Alert.Indicator />
                <Alert.Content>
                  <Alert.Title>出错了</Alert.Title>
                  <Alert.Description>{err}</Alert.Description>
                </Alert.Content>
              </Alert>
            ) : retrieving || !meta ? (
              <div className="space-y-3">
                {Array.from({ length: 4 }, (_, i) => (
                  <Skeleton key={i} className="h-[74px] rounded-2xl" />
                ))}
              </div>
            ) : (
              <Results res={synth} streaming={streaming} />
            )}
          </motion.section>
        )}
      </motion.div>
    </motion.div>
  )
}
