// Cloudflare Turnstile token provider for the paid endpoints (/api/ask*, /api/sim/advise).
// Off when VITE_TURNSTILE_SITEKEY is unset: turnstileHeaders() returns {}, so local dev and
// any deploy without the key behave exactly as before. Backend matches this (TURNSTILE_SECRET
// unset -> verification skipped).
//
// Invisible execute mode: the widget renders once hidden, and every getToken() call runs
// turnstile.execute() to mint a fresh single-use token (Turnstile tokens are single-use; reusing
// one makes siteverify fail).

const SITEKEY = import.meta.env.VITE_TURNSTILE_SITEKEY
const SCRIPT_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js'
const TOKEN_TIMEOUT_MS = 10000

interface TurnstileApi {
  render: (el: HTMLElement, opts: Record<string, unknown>) => string
  execute: (idOrEl: string | HTMLElement, opts?: Record<string, unknown>) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

let readyPromise: Promise<void> | null = null
let widgetId: string | null = null
let pending: ((token: string | null) => void) | null = null

const settle = (token: string | null) => {
  const resolve = pending
  pending = null
  if (resolve) resolve(token)
}

const ensureReady = (): Promise<void> => {
  if (readyPromise) return readyPromise
  readyPromise = new Promise<void>((resolve, reject) => {
    function mount() {
      const turnstile = window.turnstile
      if (!turnstile) {
        reject(new Error('turnstile script loaded but window.turnstile is missing'))
        return
      }
      const host = document.createElement('div')
      host.style.display = 'none'
      document.body.appendChild(host)
      widgetId = turnstile.render(host, {
        sitekey: SITEKEY,
        size: 'invisible',
        execution: 'execute',
        callback: (token: string) => settle(token),
        'error-callback': () => settle(null),
        'timeout-callback': () => settle(null),
      })
      resolve()
    }
    if (window.turnstile) {
      mount()
      return
    }
    const existing = document.querySelector<HTMLScriptElement>(`script[src="${SCRIPT_SRC}"]`)
    const script = existing ?? document.createElement('script')
    script.addEventListener('load', mount, { once: true })
    script.addEventListener('error', () => reject(new Error('failed to load turnstile script')), {
      once: true,
    })
    if (!existing) {
      script.src = SCRIPT_SRC
      script.async = true
      document.head.appendChild(script)
    }
  })
  return readyPromise
}

const getToken = async (): Promise<string | null> => {
  if (!SITEKEY) return null
  try {
    await ensureReady()
  } catch {
    return null
  }
  const turnstile = window.turnstile
  const id = widgetId
  if (!turnstile || !id) return null
  return new Promise<string | null>((resolve) => {
    const timer = window.setTimeout(() => settle(null), TOKEN_TIMEOUT_MS)
    pending = (token) => {
      window.clearTimeout(timer)
      resolve(token)
    }
    try {
      turnstile.execute(id, { action: 'ask' })
    } catch {
      settle(null)
    }
  })
}

export async function turnstileHeaders(): Promise<Record<string, string>> {
  const token = await getToken()
  return token ? { 'x-turnstile-response': token } : {}
}
