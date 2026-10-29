import { useCallback, useEffect, useState } from 'react'
import { ArrowRight, Info, ChevronDown, CircleHelp, FileText, Fingerprint, KeyRound, LogOut, Menu, MessageSquare, Plus, Search, ShieldCheck, Sparkles, Trash2, X } from 'lucide-react'
import { api, post, session } from './api'
import { Brand, CheckLine, ErrorText, Modal, Spinner } from './components'
import { ApprovalsView, MemoryView, ReportReader, ReportsView } from './DataViews'
import { WorkspaceView } from './WorkspaceView'
import { isActive, useWorkspace } from './useWorkspace'
import type { Conversation, Employee, Nav, Reference, Skill } from './types'

function Login({ onLogin, initialError }: { onLogin: (employee: Employee) => void; initialError: string }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(initialError)
  const [busy, setBusy] = useState(false)
  const submit = async () => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const result = await post<{ access_token: string; employee: Employee }>('/api/auth/login', { username: username.trim(), password })
      session.set(result.access_token); setPassword(''); onLogin(result.employee)
    } catch (error) { setError(error instanceof Error ? error.message : '登录未完成') }
    finally { setBusy(false) }
  }
  return <main className="login-page"><div className="login-top"><Brand /><span>让经营，井然有序。</span></div><div className="login-layout"><section className="login-story"><div className="welcome-pill"><span className="gold-dot" />为二手车门店而生</div><h1>从纷繁的信息，<br />抵达<em>清晰的决定。</em></h1><p>找车、核对资料、协作报价。<br />车序将你的每一个需求，化为有据可循的行动。</p><div className="login-benefits"><CheckLine>资料有出处</CheckLine><CheckLine>关键操作由你确认</CheckLine><CheckLine>工作进度随时可见</CheckLine></div><div className="login-visual" aria-hidden="true"><div className="glass-sheet sheet-back" /><div className="glass-sheet sheet-front"><span className="gold-dot" /><span className="visual-line" /><span className="visual-line short" /><div><i /><i /><i /></div></div><div className="visual-orbit" /></div></section>
      <section className="login-card"><span className="login-card-icon"><Sparkles size={24} strokeWidth={1.3} /></span><h2>欢迎回到车序</h2><p>登录你的工作空间，继续今天的任务。</p><form onSubmit={event => { event.preventDefault(); void submit() }}><label>员工账号<input required autoComplete="username" autoFocus maxLength={32} value={username} onChange={event => setUsername(event.target.value)} placeholder="输入员工账号" /></label><label>登录密码<input required autoComplete="current-password" type="password" maxLength={128} value={password} onChange={event => setPassword(event.target.value)} placeholder="输入密码" /></label><ErrorText message={error} /><button className="button primary full login-submit" disabled={busy}>{busy ? <Spinner label="正在登录" /> : <>进入工作空间<ArrowRight size={17} /></>}</button></form><p className="login-assistance"><ShieldCheck size={14} />使用管理员已创建的员工账号登录</p></section></div><footer className="login-footer"><span>CHEXU · 车序</span><span>把判断留给你，把繁杂交给车序。</span></footer></main>
}

function PasswordDialog({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [current, setCurrent] = useState(''), [next, setNext] = useState(''), [repeat, setRepeat] = useState('')
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const submit = async () => {
    if (busy) return
    if (next !== repeat) { setError('两次输入的新密码不一致。'); return }
    setBusy(true); setError('')
    try { await post('/api/auth/change-password', { current_password: current, new_password: next }); onChanged() }
    catch (error) { setError(error instanceof Error ? error.message : '密码修改未完成') }
    finally { setBusy(false) }
  }
  return <Modal title="修改登录密码" description="修改成功后需要重新登录。新密码至少12位。" onClose={() => !busy && onClose()}><form onSubmit={event => { event.preventDefault(); void submit() }}><label>当前密码<input type="password" required autoComplete="current-password" value={current} onChange={event => setCurrent(event.target.value)} /></label><label>新密码<input type="password" required autoComplete="new-password" minLength={12} maxLength={128} value={next} onChange={event => setNext(event.target.value)} /></label><label>再次输入新密码<input type="password" required autoComplete="new-password" minLength={12} maxLength={128} value={repeat} onChange={event => setRepeat(event.target.value)} /></label><ErrorText message={error} /><div className="modal-actions"><button className="button subtle" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button primary" disabled={busy}>{busy ? <Spinner /> : '确认修改'}</button></div></form></Modal>
}

const navigation = [
  { id: 'workspace', icon: MessageSquare, label: '智能工作台' }, { id: 'reports', icon: FileText, label: '车辆资料' },
  { id: 'approvals', icon: ShieldCheck, label: '审批中心' }, { id: 'memories', icon: Fingerprint, label: '偏好记忆' },
] as const

function SessionApp({ employee, onLogout }: { employee: Employee; onLogout: () => void }) {
  const [nav, setNav] = useState<Nav>('workspace')
  const [skills, setSkills] = useState<Skill[]>([])
  const [search, setSearch] = useState('')
  const [toast, setToast] = useState('')
  const [help, setHelp] = useState(false)
  const [passwordDialog, setPasswordDialog] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [deleting, setDeleting] = useState<Conversation | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [reference, setReference] = useState<Reference | null>(null)
  const [online, setOnline] = useState(true)
  const [pendingApprovals, setPendingApprovals] = useState(0)
  const [composeRequest, setComposeRequest] = useState<{ key: number; text: string } | null>(null)
  const [pendingMemoryIds, setPendingMemoryIds] = useState<string[]>([])
  const consumeCompose = useCallback(() => setComposeRequest(null), [])
  const notify = useCallback((text: string) => setToast(text), [])
  const w = useWorkspace(notify)

  useEffect(() => {
    void w.loadConversations().catch(error => notify(error.message))
    void api<Skill[]>('/api/skills').then(setSkills).catch(() => {})
    const savedThread = sessionStorage.getItem(`chexu.thread.${employee.employee_id}`)
    if (savedThread && /^[0-9a-f-]{36}$/i.test(savedThread)) void w.selectThread(savedThread)
  }, [w.loadConversations, w.selectThread, notify, employee.employee_id])
  useEffect(() => {
    const key = `chexu.thread.${employee.employee_id}`
    if (w.threadId) sessionStorage.setItem(key, w.threadId)
    else sessionStorage.removeItem(key)
  }, [w.threadId, employee.employee_id])
  useEffect(() => {
    if (!toast) return
    const timer = setTimeout(() => setToast(''), 6500)
    return () => clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    let active = true
    const check = () => api('/health').then(() => { if (active) setOnline(true) }).catch(() => { if (active) setOnline(false) })
    void check()
    const timer = setInterval(check, 30000)
    return () => { active = false; clearInterval(timer) }
  }, [])
  useEffect(() => {
    void api<{ approvals: unknown[] }>('/api/approvals?status=pending&limit=100').then(value => setPendingApprovals(value.approvals.length)).catch(() => {})
  }, [nav, w.task?.status])
  useEffect(() => {
    let active = true
    void api<{ memory_id: string }[]>('/api/memories?status=pending').then(items => { if (active) setPendingMemoryIds(items.map(item => item.memory_id)) }).catch(() => {})
    return () => { active = false }
  }, [nav, w.result?.run_id])

  const go = (value: Nav) => {
    setNav(value); setSidebarOpen(false)
    if (value === 'workspace') void w.reload().catch(error => notify(error.message))
  }
  const chooseConversation = (id: string) => { setNav('workspace'); setSidebarOpen(false); void w.selectThread(id) }
  const newConversation = () => {
    if (isActive(w.task)) notify('原任务仍在后台执行，可以从会话列表返回。')
    setNav('workspace'); setSidebarOpen(false); void w.selectThread(null)
  }
  const logout = async () => {
    try { await post('/api/auth/logout') } catch { /* 离线时仍清除本地会话。 */ }
    finally { session.clear(); onLogout() }
  }
  const selectedTitle = w.conversations.find(item => item.thread_id === w.threadId)?.title
  const filtered = w.conversations.filter(item => item.title.toLowerCase().includes(search.toLowerCase()))

  return <div className={`app-shell ${sidebarOpen ? 'sidebar-open' : ''}`}>
    <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setSidebarOpen(false)} />
    <aside className="sidebar"><div className="sidebar-brand"><Brand /><button className="icon-button mobile-close" aria-label="关闭导航" onClick={() => setSidebarOpen(false)}><X size={17} /></button></div>
      <div className="workspace-identity"><span className="workspace-monogram">序</span><div><strong>我的工作空间</strong><span>每一步，都井然有序</span></div><ShieldCheck size={14} /></div>
      <button className="new-conversation" onClick={newConversation}><Plus size={18} />新建会话<span>↗</span></button>
      <nav className="main-nav" aria-label="主导航">{navigation.map(item => <button key={item.id} className={nav === item.id ? 'active' : ''} onClick={() => go(item.id)}><item.icon size={18} strokeWidth={1.6} /><span>{item.label}</span>{item.id === 'approvals' && pendingApprovals > 0 && <small>{pendingApprovals >= 100 ? '99+' : pendingApprovals}</small>}</button>)}</nav>
      <div className="conversation-list-heading"><span>最近会话</span><span>{w.conversations.length > 0 ? w.conversations.length : '—'}</span></div>
      {w.conversations.length > 5 && <div className="conversation-search"><Search size={13} /><input aria-label="查找最近会话" placeholder="查找最近会话" value={search} onChange={event => setSearch(event.target.value)} /></div>}
      <div className="conversation-list">{filtered.length ? filtered.map(item => <div className={`conversation-item ${nav === 'workspace' && w.threadId === item.thread_id ? 'active' : ''}`} key={item.thread_id}><button onClick={() => chooseConversation(item.thread_id)}><MessageSquare size={14} /><span>{item.title}</span></button><button className="delete-conversation" onClick={() => { setDeleteError(''); setDeleting(item) }} aria-label={`删除会话：${item.title}`}><Trash2 size={13} /></button></div>) : <p className="sidebar-empty">{search ? '没有匹配的最近会话' : '新任务，从一段对话开始。'}</p>}{w.hasMore && <button className="sidebar-load" onClick={() => w.loadConversations(true).catch(error => notify(error.message))}>加载更早的会话</button>}</div>
      <div className="sidebar-footer"><div className="sidebar-tip"><Sparkles size={15} /><p>把复杂的事，<br />交给更清晰的协作。</p></div><details className="account-menu"><summary><span className="avatar">{employee.username.slice(0, 2).toUpperCase()}</span><span><strong>{employee.username}</strong><small>{employee.role === 'manager' ? '门店经理' : '门店销售'}</small></span><ChevronDown size={14} /></summary><div className="account-popover"><button onClick={() => setPasswordDialog(true)}><KeyRound size={15} />修改密码</button><button onClick={logout}><LogOut size={15} />退出登录</button></div></details></div>
    </aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-menu" aria-label="打开导航" onClick={() => setSidebarOpen(true)}><Menu size={20} /></button><span>工作空间</span><ChevronRightIcon /><strong>{navigation.find(item => item.id === nav)?.label}</strong>{nav === 'workspace' && selectedTitle && <><span className="breadcrumb-divider">/</span><span className="thread-title">{selectedTitle}</span></>}</div><div className="topbar-actions"><span className="service-status"><i className={`connection-dot ${online ? 'online' : ''}`} />{online ? '服务已连接' : '服务暂不可用'}</span><button className="icon-button" aria-label="使用说明" title="使用说明" onClick={() => setHelp(true)}><CircleHelp size={18} /></button><span className="topbar-avatar">{employee.username.slice(0, 1).toUpperCase()}</span></div></header>
      <main className={`main-content ${nav !== 'workspace' ? 'full-view' : ''}`}>
        {nav === 'workspace' && <WorkspaceView workspace={w} skills={skills} composeRequest={composeRequest} onComposeConsumed={consumeCompose} pendingMemoryIds={pendingMemoryIds} onReports={() => go('reports')} onMemories={() => go('memories')} onReference={setReference} notify={notify} />}
        {nav === 'reports' && <ReportsView notify={notify} onAsk={text => { newConversation(); setComposeRequest({ key: Date.now(), text }) }} />}
        {nav === 'approvals' && <ApprovalsView employee={employee} notify={notify} onConversation={chooseConversation} />}
        {nav === 'memories' && <MemoryView notify={notify} onConversation={chooseConversation} />}
      </main>
    </div>
    {toast && <div className="toast" role="status"><Info size={17} /><span>{toast}</span><button aria-label="关闭提示" onClick={() => setToast('')}><X size={14} /></button></div>}
    {help && <Modal title="让车序接住你的下一件事" description="从明确需求开始，重要信息随时核对。" onClose={() => setHelp(false)}><div className="help-content"><article><span>01</span><div><h3>说清你的需求</h3><p>提供预算、里程或车辆编号。需要多步骤协作时，选择输入框下方的“任务协作”。</p></div></article><article><span>02</span><div><h3>跟踪处理进度</h3><p>右侧查看步骤与查询结果。任务可以停止，失败后可以恢复；切换页面不会停止后台任务。</p></div></article><article><span>03</span><div><h3>核对，再确认</h3><p>报告结论可回到原文。报价由经理审核，偏好需你明确确认后才会长期使用。</p></div></article></div></Modal>}
    {reference && <ReportReader reportId={reference.report_id} filename={reference.filename} initialChunk={reference.chunk} quote={reference.quote} onClose={() => setReference(null)} />}
    {passwordDialog && <PasswordDialog onClose={() => setPasswordDialog(false)} onChanged={() => { session.clear(); onLogout() }} />}
    {deleting && <Modal title="删除这段会话？" description={deleting.title} onClose={() => !deleteBusy && setDeleting(null)}><p className="modal-copy">会话及其聊天记录将被删除。已保存的报价和审批记录仍会保留。正在执行或等待审批的会话需要先处理完毕。</p><ErrorText message={deleteError} /><div className="modal-actions"><button className="button subtle" disabled={deleteBusy} onClick={() => setDeleting(null)}>保留会话</button><button className="button danger" disabled={deleteBusy} onClick={async () => { setDeleteBusy(true); try { await w.removeThread(deleting.thread_id); setDeleting(null); notify('会话已删除。') } catch (error) { setDeleteError(error instanceof Error ? error.message : '删除未完成') } finally { setDeleteBusy(false) } }}>{deleteBusy ? <Spinner /> : '确认删除'}</button></div></Modal>}
  </div>
}

function ChevronRightIcon() { return <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="m4.5 3 3 3-3 3" stroke="currentColor" strokeWidth="1.2" /></svg> }

export default function App() {
  const [employee, setEmployee] = useState<Employee | null>(null)
  const [checking, setChecking] = useState(!!session.get())
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    if (session.get()) api<Employee>('/api/auth/me', { signal: controller.signal }).then(setEmployee)
      .catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : '会话读取失败') })
      .finally(() => { if (!controller.signal.aborted) setChecking(false) })
    const expired = () => { setEmployee(null); setChecking(false); setError('登录已过期，请重新登录。') }
    window.addEventListener('chexu:expired', expired)
    return () => { controller.abort(); window.removeEventListener('chexu:expired', expired) }
  }, [])
  if (checking) return <div className="boot-screen"><Brand /><Spinner label="正在打开工作空间" /></div>
  return employee ? <SessionApp key={employee.employee_id} employee={employee} onLogout={() => { setEmployee(null); setError('') }} /> : <Login initialError={error} onLogin={value => { setEmployee(value); setError('') }} />
}
