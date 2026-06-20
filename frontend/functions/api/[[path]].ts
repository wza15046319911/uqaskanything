interface Env {
  BACKEND_URL: string
}

export async function onRequest(context: {
  request: Request
  env: Env
}): Promise<Response> {
  const { request, env } = context

  if (!env.BACKEND_URL) {
    return new Response(JSON.stringify({ error: 'proxy misconfigured: BACKEND_URL missing' }), {
      status: 500,
      headers: { 'content-type': 'application/json' },
    })
  }

  const url = new URL(request.url)
  const target = env.BACKEND_URL.replace(/\/$/, '') + url.pathname + url.search

  const method = request.method
  const body = method === 'GET' || method === 'HEAD' ? undefined : await request.text()

  const upstream = await fetch(target, {
    method,
    headers: { 'content-type': request.headers.get('content-type') || 'application/json' },
    body,
  })

  const headers = new Headers()
  headers.set('content-type', upstream.headers.get('content-type') || 'application/json')
  return new Response(upstream.body, { status: upstream.status, headers })
}
