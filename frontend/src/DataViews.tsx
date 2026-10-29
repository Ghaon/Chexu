import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, ChevronRight, FileText, Fingerprint, FolderOpen, Plus, RefreshCw, ShieldCheck, Trash2, Upload, X, Pencil } from 'lucide-react'
import { api, ApiError, post } from './api'
import { Empty, ErrorText, Modal, QuoteCard, Spinner, Status, money, when } from './components'
import type { Approval, Employee, Memory, ParsedReport, ReportMeta } from './types'

type Notify = (text: string) => void
const categoryNames = { budget: '预算', usage: '用途', preferences: '偏好' }
const memoryValue = (value: Memory['value']) => typeof value === 'number' ? `¥ ${money(value)}` : Array.isArray(value) ? value.join(' · ') : value

export function MemoryView({ notify, onConversation }: { notify: Notify; onConversation: (id: string) => void }) {
  const [items, setItems] = useState<Memory[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<Memory | 'new' | null>(null)
  const [deleting, setDeleting] = useState<Memory | null>(null)
  const [category, setCategory] = useState<Memory['category']>('budget')
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState('')
  const load = useCallback(async () => {
    try { setItems(await api<Memory[]>('/api/memories')); setError('') }
    catch (error) { setError(error instanceof Error ? error.message : '偏好读取失败') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load() }, [load])
  const open = (item: Memory | 'new') => {
    setEditing(item); setError('')
    setCategory(item === 'new' ? 'budget' : item.category)
    setValue(item === 'new' ? '' : Array.isArray(item.value) ? item.value.join('\n') : String(item.value))
  }
  const perform = async (key: string, action: () => Promise<unknown>) => {
    if (busy) return
    setBusy(key); setError('')
    try { await action(); await load() }
    catch (error) {
      setError(error instanceof Error ? error.message : '操作未完成')
      if (error instanceof ApiError && error.status === 409) { await load(); setEditing(null); setDeleting(null); notify('偏好版本已变化，请重新查看后操作。') }
    } finally { setBusy('') }
  }
  const save = async () => {
    const parsed = category === 'budget' ? Number(value) : category === 'preferences' ? value.split(/[\n，,]/).map(item => item.trim()).filter(Boolean) : value.trim()
    if ((category === 'budget' && (!Number.isInteger(parsed) || Number(parsed) < 1 || Number(parsed) > 2147483647)) || (Array.isArray(parsed) && (parsed.length < 1 || parsed.length > 10))) { setError('请填写正整数预算，偏好为1～10条。'); return }
    await perform('save', async () => {
      const existing = editing !== 'new' && editing ? editing : items.find(item => item.status === 'confirmed' && item.category === category)
      if (existing) await api(`/api/memories/${existing.memory_id}`, { method: 'PATCH', body: JSON.stringify({ value: parsed, expected_revision: existing.revision, confirm: true }) })
      else await post('/api/memories', { category, value: parsed, confirm: true })
      setEditing(null); notify('偏好已确认，后续任务会使用最新内容。')
    })
  }
  const confirmed = items.filter(item => item.status === 'confirmed')
  const pending = items.filter(item => item.status === 'pending')
  return <section className="page-view"><div className="page-heading"><div><span className="page-icon"><Fingerprint size={21} /></span><h1>让车序更懂你的工作</h1><p>明确确认的预算、用途和偏好，会成为后续任务的参考。</p></div><button className="button primary" onClick={() => open('new')}><Plus size={16} />添加偏好</button></div>
    {!editing && !deleting && <ErrorText message={error} />}
    <div className="section-heading"><h2>已确认的偏好</h2><span className="caption">仅当前账号可见</span></div>
    {loading ? <Spinner label="正在读取偏好" /> : <div className="memory-grid">{(['budget', 'usage', 'preferences'] as const).map(key => {
      const record = confirmed.find(item => item.category === key)
      return <article className="memory-card" key={key}><div className="section-heading"><span>{categoryNames[key]}</span>{record ? <Check size={16} className="good-text" /> : <span className="caption">未设置</span>}</div><strong className={key === 'budget' ? 'gold-text' : ''}>{record ? memoryValue(record.value) : '留给下一次明确的选择'}</strong><p>{record ? `更新于 ${when(record.updated_at)}` : '没有记录时，车序会向你确认条件。'}</p><div className="button-row"><button className="text-button" onClick={() => { if (record) open(record); else { open('new'); setCategory(key) } }}><Pencil size={13} />{record ? '修改' : '添加'}</button>{record && <button className="icon-button danger-text" aria-label={`删除${categoryNames[key]}`} onClick={() => { setError(''); setDeleting(record) }}><Trash2 size={14} /></button>}</div></article>
    })}</div>}
    <div className="section-heading spaced"><h2>等待你的确认 <span className="count-tag">{pending.length}</span></h2><span className="caption">候选内容尚未生效</span></div>
    {pending.length ? <div className="pending-memories">{pending.map(item => <article className="pending-memory" key={item.memory_id}><div><Status value="pending" /><h3>{categoryNames[item.category]} · {memoryValue(item.value)}</h3>{item.source_quote && <blockquote>“{item.source_quote}”</blockquote>}<p className="caption">确认后替换同类别的已确认内容，不会自动建立客户档案。</p></div><div className="memory-actions"><button className="button small primary" disabled={!!busy} onClick={() => perform(item.memory_id, async () => { await post(`/api/memories/${item.memory_id}/confirm`, { confirm: true, expected_revision: item.revision }); notify('这条偏好已确认。') })}>{busy === item.memory_id ? <Spinner label="" /> : <Check size={14} />}确认</button><button className="button small subtle" onClick={() => open(item)}>修改后确认</button><button className="icon-button" title="丢弃候选" aria-label="丢弃候选" onClick={() => { setError(''); setDeleting(item) }}><X size={16} /></button>{item.source_thread_id && <button className="text-button" onClick={() => onConversation(item.source_thread_id!)}>查看会话<ChevronRight size={13} /></button>}</div></article>)}</div> : !loading && <div className="quiet-empty"><ShieldCheck size={20} /><p>没有待确认的内容。你在对话中明确要求记住时，候选偏好会出现在这里。</p></div>}
    <p className="page-note">历史中的临时推测不会自动变成已确认偏好。删除偏好不会清除历史聊天记录。</p>
    {editing && <Modal title={editing === 'new' ? '添加并确认偏好' : '修改并确认偏好'} description="请只记录当前账号的工作条件，客户的具体需求建议保留在对应会话中。" onClose={() => !busy && setEditing(null)}><form onSubmit={event => { event.preventDefault(); void save() }}>
      <label>类别<select value={category} disabled={editing !== 'new'} onChange={event => { setCategory(event.target.value as Memory['category']); setValue('') }}><option value="budget">预算</option><option value="usage">用途</option><option value="preferences">偏好</option></select></label>
      <label>{category === 'budget' ? '预算上限（元）' : category === 'usage' ? '使用场景' : '偏好（每行一条，最多10条）'}{category === 'budget' ? <input autoFocus type="number" required min={1} max={2147483647} step={1} value={value} onChange={event => setValue(event.target.value)} placeholder="例如：100000" /> : <textarea autoFocus required rows={4} maxLength={category === 'usage' ? 200 : 1000} value={value} onChange={event => setValue(event.target.value)} placeholder={category === 'usage' ? '例如：日常通勤、家庭接送' : '例如：自动挡\n优先考虑使用成本'} />}</label>
      {editing === 'new' && confirmed.some(item => item.category === category) && <p className="inline-hint">确认保存后，会替换当前已确认的{categoryNames[category]}。</p>}<ErrorText message={error} /><div className="modal-actions"><button className="button subtle" type="button" disabled={!!busy} onClick={() => setEditing(null)}>取消</button><button className="button primary" disabled={!!busy}>{busy ? <Spinner /> : '确认并保存'}</button></div>
    </form></Modal>}
    {deleting && <Modal title={deleting.status === 'pending' ? '丢弃这条候选？' : '删除这条偏好？'} description={`${categoryNames[deleting.category]}：${memoryValue(deleting.value)}`} onClose={() => !busy && setDeleting(null)}><p className="modal-copy">这条内容将不再作为长期偏好使用，历史聊天记录仍然保留。</p><ErrorText message={error} /><div className="modal-actions"><button className="button subtle" disabled={!!busy} onClick={() => setDeleting(null)}>保留</button><button className="button danger" disabled={!!busy} onClick={() => perform('delete', async () => { await api(`/api/memories/${deleting.memory_id}?expected_revision=${deleting.revision}`, { method: 'DELETE' }); setDeleting(null); notify('内容已删除。') })}>{busy ? <Spinner /> : '确认删除'}</button></div></Modal>}
  </section>
}

export function ApprovalsView({ employee, notify, onConversation }: { employee: Employee; notify: Notify; onConversation: (id: string) => void }) {
  const [filter, setFilter] = useState('pending')
  const filterRef = useRef(filter); filterRef.current = filter
  const [items, setItems] = useState<Approval[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [action, setAction] = useState<{ item: Approval; decision: 'approve' | 'reject' | 'cancel' } | null>(null)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const load = async (append = false) => {
    const selected = filterRef.current
    try {
      const result = await api<{ approvals: Approval[]; has_more: boolean }>(`/api/approvals?limit=30&offset=${append ? items.length : 0}${selected ? `&status=${selected}` : ''}`)
      if (filterRef.current !== selected) return
      setItems(prev => append ? [...prev, ...result.approvals] : result.approvals); setHasMore(result.has_more)
    } catch (error) { setError(error instanceof Error ? error.message : '审批读取失败') }
    finally { setLoading(false) }
  }
  useEffect(() => { setItems([]); setLoading(true); setError(''); void load() }, [filter])
  const submit = async () => {
    if (!action || busy) return
    setBusy(true); setError('')
    try {
      const result = await post<{ execution_status: string }>(`/api/approvals/${action.item.approval_id}/${action.decision === 'cancel' ? 'cancel' : 'review'}`,
        action.decision === 'cancel' ? undefined : { decision: action.decision, note })
      notify(result.execution_status === 'failed' ? '审核决定已记录，后续执行暂未完成，请查看原任务。' : action.decision === 'approve' ? '审核已通过，系统已处理后续任务。' : '审核决定已记录。')
      setAction(null); await load()
    } catch (error) { setError(error instanceof Error ? error.message : '审核未完成') }
    finally { setBusy(false) }
  }
  return <section className="page-view"><div className="page-heading"><div><span className="page-icon"><ShieldCheck size={21} /></span><h1>关键决定，留给你确认</h1><p>{employee.role === 'manager' ? '核对车辆与优惠金额，审核后推进后续任务。' : '查看你提交的报价，以及经理的审核结果。'}</p></div><button className="button subtle" onClick={() => load()}><RefreshCw size={15} />刷新</button></div>
    <div className="filter-tabs">{[['pending', '等待审核'], ['completed', '已完成'], ['', '全部记录']].map(([value, label]) => <button key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div>
    {!action && <ErrorText message={error} />}{loading ? <Spinner label="正在读取审批" /> : items.length ? <div className="approval-grid">{items.map(item => <article className="approval-card" key={item.approval_id}><div className="section-heading"><Status value={item.status} /><span className="caption">{when(item.created_at)}</span></div><QuoteCard quote={item.quote_draft} saved={!!item.quote_id} /><div className="approval-meta"><span>提交人 <strong>{item.requested_by_username}</strong></span>{item.review_note && <p>审核说明：{item.review_note}</p>}{item.failure_reason && <p className="danger-text">{item.failure_reason}</p>}</div><div className="button-row">
      {employee.role === 'manager' && item.status === 'pending' && <><button className="button primary small" onClick={() => { setError(''); setNote(''); setAction({ item, decision: 'approve' }) }}>核对并通过</button><button className="button subtle small" onClick={() => { setError(''); setNote(''); setAction({ item, decision: 'reject' }) }}>拒绝</button></>}
      {item.requested_by_employee_id === employee.employee_id && <>{item.status === 'pending' && <button className="button subtle small" onClick={() => { setError(''); setAction({ item, decision: 'cancel' }) }}>撤回申请</button>}{item.thread_id && <button className="text-button" onClick={() => onConversation(item.thread_id!)}>返回会话<ChevronRight size={14} /></button>}</>}
    </div></article>)}</div> : <Empty title="当前没有审批记录">需要经理确认的报价，会出现在这里。</Empty>}
    {hasMore && <button className="button subtle load-more" onClick={() => load(true)}>加载更多记录</button>}
    {action && <Modal title={action.decision === 'approve' ? '确认通过这笔报价？' : action.decision === 'reject' ? '拒绝这笔报价' : '撤回报价申请'} description="请核对车辆、优惠和报价金额。审核动作会记录在任务中。" onClose={() => !busy && setAction(null)}><QuoteCard quote={action.item.quote_draft} /><form onSubmit={event => { event.preventDefault(); void submit() }}>{action.decision !== 'cancel' && <label>审核说明<textarea rows={3} value={note} onChange={event => setNote(event.target.value)} maxLength={500} placeholder="可填写通过或拒绝的原因" /></label>}<ErrorText message={error} /><div className="modal-actions"><button className="button subtle" type="button" disabled={busy} onClick={() => setAction(null)}>再检查一下</button><button className={`button ${action.decision === 'approve' ? 'primary' : 'danger'}`} disabled={busy}>{busy ? <Spinner /> : action.decision === 'approve' ? '确认通过' : action.decision === 'reject' ? '确认拒绝' : '确认撤回'}</button></div></form></Modal>}
  </section>
}

export function ReportReader({ reportId, filename, initialChunk = 1, quote, onClose }: { reportId: string; filename: string; initialChunk?: number; quote?: string; onClose: () => void }) {
  const [start, setStart] = useState(initialChunk)
  const [data, setData] = useState<ParsedReport | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const limit = quote ? 1 : 3
  useEffect(() => {
    const controller = new AbortController(); setData(null); setError('')
    api<ParsedReport>(`/api/reports/${reportId}/content?start_chunk=${start}&limit=${limit}`, { signal: controller.signal })
      .then(setData).catch(error => { if (!controller.signal.aborted) setError(error instanceof Error ? error.message : '原文读取失败') })
    return () => controller.abort()
  }, [reportId, start, retry, limit])
  return <Modal title="核对报告原文" description={filename} onClose={onClose}><div className="report-reader"><ErrorText message={error} />{error ? <button className="button subtle" onClick={() => setRetry(retry + 1)}>重新读取</button> : !data ? <div className="loading-center"><Spinner label="正在读取资料" /></div> : <>{data.chunks.map(chunk => {
    const index = quote ? chunk.text.indexOf(quote) : -1
    return <section key={chunk.number}><div className="section-heading"><strong>片段 {chunk.number}</strong><span className="caption">共 {data.total_chunks} 个片段</span></div><p>{index >= 0 ? <>{chunk.text.slice(0, index)}<mark>{quote}</mark>{chunk.text.slice(index + quote!.length)}</> : chunk.text}</p></section>
  })}<div className="reader-pagination"><button className="button subtle small" disabled={start <= 1} onClick={() => setStart(Math.max(1, start - limit))}><ArrowLeft size={14} />上一段</button><span className="caption">原文片段 · 不代表实车鉴定</span><button className="button subtle small" disabled={!data.has_more} onClick={() => setStart(start + data.chunks.length)}>下一段<ArrowRight size={14} /></button></div>{data.truncated && <p className="inline-hint">文字提取已达到长度上限，当前内容未覆盖完整报告。</p>}</>}</div></Modal>
}

export function ReportsView({ notify, onAsk }: { notify: Notify; onAsk: (text: string) => void }) {
  const [vehicleId, setVehicleId] = useState('')
  const [selected, setSelected] = useState('')
  const [reports, setReports] = useState<ReportMeta[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [reader, setReader] = useState<ReportMeta | null>(null)
  const [creating, setCreating] = useState(false)
  const [vehicle, setVehicle] = useState({ vehicle_id: '', title: '', price_cny: '', mileage_km: '' })
  const fileInput = useRef<HTMLInputElement>(null)
  const load = async (id: string, append = false) => {
    if (!id.trim()) return
    setBusy('search'); setError('')
    try {
      const result = await api<{ reports: ReportMeta[]; has_more: boolean }>(`/api/vehicles/${encodeURIComponent(id.trim())}/reports?limit=20&offset=${append ? reports.length : 0}`)
      setReports(prev => append ? [...prev, ...result.reports] : result.reports); setSelected(id.trim()); setHasMore(result.has_more)
    } catch (error) { setError(error instanceof Error ? error.message : '资料查询失败'); if (!append) { setSelected(''); setReports([]) } }
    finally { setBusy('') }
  }
  const upload = async (file?: File) => {
    if (!file || !selected) return
    if (file.size > 10 * 1024 * 1024) { setError('文件超过10MB，请压缩后上传。'); return }
    setBusy('upload'); setError('')
    try {
      const body = new FormData(); body.append('file', file)
      await api(`/api/vehicles/${encodeURIComponent(selected)}/reports`, { method: 'POST', body })
      notify('资料已上传，可以交给车序解读。'); await load(selected)
    } catch (error) { setError(error instanceof Error ? error.message : '上传未完成') }
    finally { setBusy(''); if (fileInput.current) fileInput.current.value = '' }
  }
  const create = async () => {
    setBusy('create'); setError('')
    try {
      const id = vehicle.vehicle_id.trim()
      await post('/api/vehicles', { ...vehicle, vehicle_id: id, title: vehicle.title.trim(), price_cny: Number(vehicle.price_cny), mileage_km: Number(vehicle.mileage_km) })
      setCreating(false); setVehicleId(id); await load(id); notify('车辆已录入，可以关联检测资料。')
    } catch (error) { setError(error instanceof Error ? error.message : '车辆录入未完成') }
    finally { setBusy('') }
  }
  return <section className="page-view"><div className="page-heading"><div><span className="page-icon"><FolderOpen size={21} /></span><h1>把资料，变成决策依据</h1><p>按车辆关联报告，让每条结论都能找到出处。</p></div><button className="button subtle" onClick={() => { setError(''); setCreating(true) }}><Plus size={16} />录入车辆</button></div>
    <form className="vehicle-search" onSubmit={event => { event.preventDefault(); void load(vehicleId) }}><label>车辆编号<input value={vehicleId} onChange={event => { setVehicleId(event.target.value); setSelected(''); setReports([]) }} required maxLength={32} placeholder="输入已有车辆编号，例如 CX-001" /></label><button className="button primary" disabled={!!busy}>{busy === 'search' ? <Spinner label="查询中" /> : '查找资料'}</button></form>
    {!creating && <ErrorText message={error} />}
    {selected ? <><div className="section-heading spaced"><h2>{selected} 的车辆资料</h2><div className="button-row"><button className="button subtle small" onClick={() => onAsk(`请读取车辆 ${selected} 的检测报告，核对事实、未知信息和需要复检的事项。`)} disabled={!reports.length}>交给车序解读<ArrowRight size={14} /></button><button className="button primary small" disabled={!!busy} onClick={() => fileInput.current?.click()}>{busy === 'upload' ? <Spinner label="上传中" /> : <><Upload size={14} />上传报告</>}</button></div></div>
      <input ref={fileInput} type="file" hidden accept=".pdf,.docx,.txt,.md" onChange={event => upload(event.target.files?.[0])} />
      {reports.length ? <div className="report-list">{reports.map(item => <button className="report-row" key={item.report_id} onClick={() => setReader(item)}><span className="report-file-icon"><FileText size={24} strokeWidth={1.4} /></span><span><strong>{item.filename}</strong><small>{item.suffix.slice(1).toUpperCase()} · {(item.size_bytes / 1024).toFixed(1)} KB · {when(item.created_at)}</small></span><span className="text-button">查看原文<ChevronRight size={15} /></span></button>)}</div> : <Empty title="这辆车还没有关联资料">上传报告后，可以让车序基于原文进行解读。</Empty>}{hasMore && <button className="button subtle load-more" disabled={!!busy} onClick={() => load(selected, true)}>加载更多资料</button>}
    </> : <div className="report-intro"><div className="document-art"><span /><FileText size={53} strokeWidth={.8} /><i /></div><h2>让信息有条理，让判断有依据</h2><p>输入车辆编号，查看已关联的检测资料。<br />首次使用时，可以先录入车辆，再上传报告。</p><div className="format-tags"><span>PDF</span><span>DOCX</span><span>TXT</span><span>MD</span></div></div>}
    <p className="page-note">单份文件最多10MB。支持文字型文档；扫描件暂不支持识别。上传关联不代表已经核验报告真实性或车辆身份。</p>
    {reader && <ReportReader reportId={reader.report_id} filename={reader.filename} onClose={() => setReader(null)} />}
    {creating && <Modal title="录入车辆" description="填写当前库存资料。价格单位为人民币元，里程单位为公里。" onClose={() => !busy && setCreating(false)}><form onSubmit={event => { event.preventDefault(); void create() }}>
      <label>车辆编号<input autoFocus required maxLength={32} value={vehicle.vehicle_id} onChange={event => setVehicle({ ...vehicle, vehicle_id: event.target.value })} placeholder="例如 CX-001" /></label><label>车辆名称<input required maxLength={200} value={vehicle.title} onChange={event => setVehicle({ ...vehicle, title: event.target.value })} placeholder="品牌、车系和版本" /></label><div className="form-columns"><label>当前售价（元）<input type="number" required min={0} max={2147483647} step={1} value={vehicle.price_cny} onChange={event => setVehicle({ ...vehicle, price_cny: event.target.value })} /></label><label>当前里程（公里）<input type="number" required min={0} max={2147483647} step={1} value={vehicle.mileage_km} onChange={event => setVehicle({ ...vehicle, mileage_km: event.target.value })} /></label></div><ErrorText message={error} /><div className="modal-actions"><button className="button subtle" type="button" disabled={!!busy} onClick={() => setCreating(false)}>取消</button><button className="button primary" disabled={!!busy}>{busy ? <Spinner /> : '确认录入'}</button></div>
    </form></Modal>}
  </section>
}
