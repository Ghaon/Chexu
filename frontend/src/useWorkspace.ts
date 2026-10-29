import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, post, watchTask } from './api'
import type { ChatResult, Conversation, Message, Plan, Task, TaskEvent } from './types'

export const isActive = (task: Task | null) => !!task && ['queued', 'running', 'cancel_requested'].includes(task.status)
type ContextInfo = { digest: { covered_messages: number } | null; usage: { input_estimated_tokens: number; input_limit: number } | null }

export function useWorkspace(notify: (message: string) => void) {
  const [conversations, setConversations] = useState<Conversation[]>([])
  const conversationsRef = useRef(conversations); conversationsRef.current = conversations
  const [hasMore, setHasMore] = useState(false)
  const [threadId, setThreadId] = useState<string | null>(null)
  const selection = useRef<string | null>(null)
  const selectionVersion = useRef(0)
  const [messages, setMessages] = useState<Message[]>([])
  const [task, setTask] = useState<Task | null>(null)
  const taskRef = useRef<Task | null>(null)
  const setCurrentTask = useCallback((value: Task | null) => { taskRef.current = value; setTask(value) }, [])
  const [result, setResult] = useState<ChatResult | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [contextInfo, setContextInfo] = useState<ContextInfo | null>(null)
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [connected, setConnected] = useState(true)
  const [loading, setLoading] = useState(false)
  const [sending, setSending] = useState(false)
  const sendLock = useRef(false)
  const [pendingCommand, setPendingCommand] = useState<{ thread: string; id: string } | null>(null)
  const cursors = useRef(new Map<string, number>())

  const loadConversations = useCallback(async (append = false) => {
    const offset = append ? conversationsRef.current.length : 0
    const page = await api<{ conversations: Conversation[]; has_more: boolean }>(`/api/conversations?limit=30&offset=${offset}`)
    setConversations(prev => append ? [...prev, ...page.conversations.filter(item => !prev.some(old => old.thread_id === item.thread_id))] : page.conversations)
    setHasMore(page.has_more)
  }, [])

  const loadDetails = useCallback(async (id: string, current: Task | null) => {
    const update = <T,>(setter: (value: T) => void, value: T) => {
      if (selection.current === id && taskRef.current?.task_id === current?.task_id && taskRef.current?.run_id === current?.run_id) setter(value)
    }
    const [nextPlan] = await Promise.all([
      api<Plan | null>(`/api/conversations/${id}/plan`),
      !isActive(current) ? api<{ messages: Message[] }>(`/api/conversations/${id}`).then(history => update(setMessages, history.messages)) : Promise.resolve(),
    ])
    update(setPlan, nextPlan)
    if (current && !isActive(current)) {
      const response = await api<{ result: ChatResult | null }>(`/api/tasks/${current.task_id}/result`)
      update(setResult, response.result)
    }
    if (!isActive(current)) {
      const info = await api<ContextInfo>(`/api/conversations/${id}/context`)
      update(setContextInfo, info)
    }
  }, [])

  const selectThread = useCallback(async (id: string | null) => {
    selection.current = id
    selectionVersion.current += 1
    setLoading(false)
    setThreadId(id); setMessages([]); setCurrentTask(null); setResult(null); setPlan(null); setEvents([]); setContextInfo(null); setPendingCommand(null)
    if (!id) return
    setLoading(true)
    try {
      const tasks = await api<Task[]>(`/api/tasks?thread_id=${id}&limit=10`)
      if (selection.current !== id) return
      const current = tasks[0] || null
      setCurrentTask(current)
      if (current) {
        const page = await api<{ events: TaskEvent[]; next_sequence: number }>(`/api/tasks/${current.task_id}/events?limit=100`)
        if (selection.current !== id) return
        setEvents(page.events.slice(-80))
        cursors.current.set(current.task_id, page.next_sequence)
      }
      await loadDetails(id, current)
    } catch (error) { if (selection.current === id) notify(error instanceof Error ? error.message : '会话读取失败') }
    finally { if (selection.current === id) setLoading(false) }
  }, [loadDetails, notify, setCurrentTask])

  const refresh = useCallback(async (id: string, taskId: string) => {
    const previousRun = taskRef.current?.run_id
    const current = await api<Task>(`/api/tasks/${taskId}`)
    if (selection.current !== id || taskRef.current?.task_id !== taskId) return false
    if (taskRef.current.run_id !== previousRun && current.run_id !== taskRef.current.run_id) return false
    setCurrentTask(current)
    await loadDetails(id, current)
    return isActive(current)
  }, [loadDetails, setCurrentTask])

  useEffect(() => {
    if (!task || !isActive(task) || !threadId) return
    const controller = new AbortController()
    const id = threadId, taskId = task.task_id
    let scheduled: ReturnType<typeof setTimeout> | undefined
    const sync = async () => selection.current === id && await refresh(id, taskId)
    const queueSync = () => {
      if (!scheduled) scheduled = setTimeout(() => {
        scheduled = undefined
        void sync().catch(() => setConnected(false))
      }, 800)
    }
    void watchTask(taskId, cursors.current.get(taskId) || 0, controller.signal, event => {
      if (selection.current !== id) return
      cursors.current.set(taskId, event.sequence)
      setEvents(prev => [...prev.filter(item => item.sequence !== event.sequence), event].slice(-80))
      queueSync()
    }, sync, setConnected)
    return () => { controller.abort(); if (scheduled) clearTimeout(scheduled) }
  }, [task?.task_id, task?.run_id, isActive(task), threadId, refresh])

  useEffect(() => {
    if (!pendingCommand) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    const check = async () => {
      try {
        const command = await api<{ status: string; task_id: string | null }>(`/api/conversations/${pendingCommand.thread}/plan/actions/${pendingCommand.id}`)
        if (cancelled || selection.current !== pendingCommand.thread) return
        if (command.task_id) await refresh(pendingCommand.thread, command.task_id)
        if (command.status !== 'queued') {
          setPendingCommand(null)
          if (command.status !== 'applied') notify('调整未生效，请刷新计划后重试。')
          return
        }
      } catch (error) {
        if (error instanceof ApiError && [401, 404].includes(error.status)) { setPendingCommand(null); return }
      }
      if (!cancelled) timer = setTimeout(check, 1500)
    }
    void check()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [pendingCommand, refresh, notify])

  const send = async (message: string, skillId: string) => {
    if (sendLock.current || isActive(taskRef.current) || taskRef.current?.status === 'awaiting_approval' || pendingCommand) return false
    sendLock.current = true; setSending(true)
    try {
      const id = selection.current || crypto.randomUUID()
      const version = selectionVersion.current
      const submitted = await post<Task>('/api/tasks', { thread_id: id, message, skill_id: skillId || undefined })
      if (selectionVersion.current !== version) { void loadConversations().catch(() => {}); return true }
      selection.current = id; setThreadId(id)
      setMessages(prev => [...prev, { role: 'user', content: message }]); setResult(null); setPlan(null); setEvents([])
      setCurrentTask(submitted); setConnected(true)
      void loadConversations().catch(() => {})
      return true
    } catch (error) { notify(error instanceof Error ? error.message : '发送失败'); return false }
    finally { setSending(false); sendLock.current = false }
  }

  const resume = async () => {
    if (!task || !threadId) return
    const id = threadId
    const response = await post<ChatResult>(`/api/runs/${task.run_id}/resume`)
    if (selection.current !== id) return
    setResult(response)
    await refresh(id, task.task_id)
  }

  const cancel = async () => {
    if (!task || !threadId) return
    if (isActive(task)) {
      const cancelled = await post<Task>(`/api/tasks/${task.task_id}/cancel`)
      if (selection.current === threadId && taskRef.current?.task_id === task.task_id) setCurrentTask(cancelled)
    }
    else { await post(`/api/runs/${task.run_id}/cancel`); await refresh(threadId, task.task_id) }
  }

  const adjust = async (action: string, message: string) => {
    if (!plan || !threadId || pendingCommand) return
    const commandId = crypto.randomUUID()
    await post(`/api/conversations/${threadId}/plan/actions`, {
      command_id: commandId, expected_revision: plan.revision, expected_turn_id: plan.turn_id, action, message,
    })
    setPendingCommand({ thread: threadId, id: commandId })
  }

  const removeThread = async (id: string) => {
    await api(`/api/conversations/${id}`, { method: 'DELETE' })
    if (selection.current === id) await selectThread(null)
    await loadConversations()
  }

  return { conversations, hasMore, loadConversations, threadId, selectThread, messages, task, result, plan,
    contextInfo, events, connected, loading, sending, pendingCommand, send, resume, cancel, adjust, removeThread,
    reload: () => threadId && task ? refresh(threadId, task.task_id) : Promise.resolve(false) }
}

export type Workspace = ReturnType<typeof useWorkspace>
