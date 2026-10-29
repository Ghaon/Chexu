import type { TaskEvent } from './types'

const TOKEN_KEY = 'chexu.session'
export const session = {
  get: () => sessionStorage.getItem(TOKEN_KEY),
  set: (value: string) => sessionStorage.setItem(TOKEN_KEY, value),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  const token = session.get()
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  let response: Response
  try { response = await fetch(path, { ...init, headers }) }
  catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError(0, '暂时无法连接服务，请确认后端已启动。')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = body.detail
    const message = typeof detail === 'string' ? detail : detail?.message || (Array.isArray(detail) ? '请检查输入内容和格式。' : '操作未完成，请稍后重试。')
    if (response.status === 401 && path !== '/api/auth/login') {
      session.clear()
      window.dispatchEvent(new Event('chexu:expired'))
    }
    throw new ApiError(response.status, message)
  }
  if (response.status === 204) return undefined as T
  return response.json()
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

const pause = (ms: number, signal: AbortSignal) => new Promise<void>((resolve, reject) => {
  const done = () => { signal.removeEventListener('abort', aborted); resolve() }
  const timer = window.setTimeout(done, ms)
  const aborted = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
  signal.addEventListener('abort', aborted, { once: true })
  if (signal.aborted) aborted()
})

export async function watchTask(taskId: string, cursor: number, signal: AbortSignal,
  onEvent: (event: TaskEvent) => void, onSync: () => Promise<boolean>, onConnection: (connected: boolean) => void) {
  // 使用 fetch 读取 SSE，携带 Bearer 身份；游标跨连接保留，重复事件不再展示。
  let delay = 800
  while (!signal.aborted) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
    try {
      const response = await fetch(`/api/tasks/${taskId}/events/stream?after=${cursor}`, {
        signal, headers: { Authorization: `Bearer ${session.get() || ''}`, Accept: 'text/event-stream' },
      })
      if (response.status === 401) { session.clear(); window.dispatchEvent(new Event('chexu:expired')); return }
      if (!response.ok || !response.body) throw new Error('stream unavailable')
      onConnection(true)
      reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
        let boundary: number
        while ((boundary = buffer.indexOf('\n\n')) >= 0) {
          const block = buffer.slice(0, boundary)
          buffer = buffer.slice(boundary + 2)
          const data = block.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
          if (data) {
            const event = JSON.parse(data)
            if (typeof event.sequence === 'number' && event.sequence > cursor) {
              cursor = event.sequence
              onEvent(event)
            }
          }
        }
      }
      if (!(await onSync())) return
      delay = 800
    } catch (error) {
      if (signal.aborted) return
      onConnection(false)
      try { if (!(await onSync())) return } catch { /* 服务恢复后继续按游标重连。 */ }
      delay = Math.min(delay * 2, 8000)
    } finally { if (signal.aborted) await reader?.cancel().catch(() => {}) }
    await pause(delay, signal).catch(() => {})
  }
}
