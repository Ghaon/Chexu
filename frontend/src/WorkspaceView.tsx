import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDown, ArrowRight, ArrowUp, BookOpen, ChevronDown, ChevronRight, Compass, FileCheck2, FileText, Fingerprint, Layers3, ListChecks, MessageSquare, Plus, RefreshCw, Search, ShieldCheck, SlidersHorizontal, Sparkles, Square, Waypoints, X } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Workspace } from './useWorkspace'
import { isActive } from './useWorkspace'
import type { Reference, Skill } from './types'
import { Empty, ErrorText, Modal, QuoteCard, Spinner, Status, StepIcon, VehicleCard, eventName, skillNames, when } from './components'

const suggestions = [
  { icon: Search, title: '按预算找车', detail: '从库存里，找到合适的选择', text: '帮我筛选预算10万元以内、里程不超过8万公里的库存车辆。', skill: '' },
  { icon: Layers3, title: '对比两辆车', detail: '把价格、里程和取舍摆清楚', text: '请比较这两辆车的当前售价和里程：', skill: 'vehicle_compare' },
  { icon: FileCheck2, title: '解读检测资料', detail: '每一条结论，都能回到原文', text: '请读取这辆车的检测报告，列出事实、未知信息和需要复检的事项：', skill: 'vehicle_report' },
  { icon: ShieldCheck, title: '协作生成报价', detail: '先预览，关键操作再确认', text: '请为这辆车生成报价预览，先不要提交审批：', skill: 'quote_assist' },
]

function CarArtwork() {
  return <svg className="car-artwork" viewBox="0 0 360 180" fill="none" aria-hidden="true"><defs><linearGradient id="carline"><stop stopColor="#e4b863" stopOpacity=".15" /><stop offset=".6" stopColor="#e4b863" stopOpacity=".85" /><stop offset="1" stopColor="#adc5de" stopOpacity=".15" /></linearGradient></defs>
    <ellipse cx="186" cy="142" rx="142" ry="18" stroke="#a9bed7" strokeOpacity=".09" /><ellipse cx="186" cy="142" rx="105" ry="11" stroke="#e4b863" strokeOpacity=".12" />
    <path d="M38 121l8-23 46-10 34-32c8-7 19-10 33-10h63c19 0 39 11 66 37l31 9 10 23-4 13h-25m-47 0H112m-47 0H42l-4-7Z" stroke="url(#carline)" strokeWidth="1.5" />
    <path d="m106 87 30-27h34l-2 27h-62Zm72-27h38c15 0 29 8 50 27h-91l3-27Z" stroke="#b4c4d8" strokeOpacity=".3" />
    <path d="M47 102h24l-8 8H43m251-13 24 7m-141-8-4 26m43-26-3 26" stroke="#b4c4d8" strokeOpacity=".18" />
    <circle cx="89" cy="127" r="23" stroke="url(#carline)" /><circle cx="89" cy="127" r="13" stroke="#b4c4d8" strokeOpacity=".25" /><circle cx="278" cy="127" r="23" stroke="url(#carline)" /><circle cx="278" cy="127" r="13" stroke="#b4c4d8" strokeOpacity=".25" />
    <path d="M24 166h310M48 28h40m228 16h24" stroke="#b4c4d8" strokeOpacity=".12" /><circle cx="307" cy="32" r="3" fill="#e4b863" fillOpacity=".5" />
  </svg>
}

export function WorkspaceView({ workspace: w, skills, composeRequest, onComposeConsumed, pendingMemoryIds, onReports, onMemories, onReference, notify }: {
  workspace: Workspace; skills: Skill[]; onReports: () => void; onMemories: () => void;
  composeRequest: { key: number; text: string } | null;
  onComposeConsumed: () => void; pendingMemoryIds: string[];
  onReference: (reference: Reference) => void; notify: (message: string) => void;
}) {
  const [draft, setDraft] = useState('')
  const [skill, setSkill] = useState('')
  const [panel, setPanel] = useState<'progress' | 'sources'>('progress')
  const [showPanel, setShowPanel] = useState(false)
  const [adjusting, setAdjusting] = useState(false)
  const [adjustText, setAdjustText] = useState('')
  const [adjustMode, setAdjustMode] = useState('revise')
  const [operation, setOperation] = useState('')
  const [opError, setOpError] = useState('')
  const composer = useRef<HTMLTextAreaElement>(null)
  const bottom = useRef<HTMLDivElement>(null)
  const scrollArea = useRef<HTMLDivElement>(null)
  const [nearBottom, setNearBottom] = useState(true)
  const [visibleCount, setVisibleCount] = useState(40)
  const running = isActive(w.task)
  const waiting = w.task?.status === 'awaiting_approval'
  const failed = !!w.task && ['failed', 'timed_out', 'interrupted'].includes(w.task.status)
  const allVisible = useMemo(() => w.messages.filter(message => message.role !== 'tool' && typeof message.content === 'string' && message.content.trim()), [w.messages])
  const visible = allVisible.slice(-visibleCount)
  const isWelcome = !visible.length && !w.task && !w.loading
  const choose = (text: string, selected?: string) => { setDraft(text); if (selected !== undefined) setSkill(selected); composer.current?.focus() }

  useEffect(() => { setDraft(''); setOpError(''); setVisibleCount(40) }, [w.threadId])
  useEffect(() => {
    if (!showPanel) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setShowPanel(false) }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [showPanel])
  useEffect(() => {
    if (composeRequest) { setDraft(composeRequest.text); setSkill('vehicle_report'); composer.current?.focus(); onComposeConsumed() }
  }, [composeRequest?.key])
  useEffect(() => { if (nearBottom) bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [visible.length, w.task?.status])

  const perform = async (name: string, action: () => Promise<unknown>) => {
    if (operation) return
    setOperation(name); setOpError('')
    try { await action() } catch (error) { setOpError(error instanceof Error ? error.message : '操作未完成') }
    finally { setOperation('') }
  }
  const submit = async () => {
    const text = draft.trim()
    if (!text || text.length > 2000) return
    setNearBottom(true)
    if (w.plan?.status === 'needs_input') {
      await perform('continue', async () => { await w.adjust('continue', text); setDraft('') })
    } else if (await w.send(text, skill)) setDraft('')
  }
  const vehicles = w.result?.vehicles?.length ? w.result.vehicles : w.result?.vehicle ? [w.result.vehicle] : []
  const analysis = w.result?.report_analysis
  const webSources = w.result?.web_sources || []
  const webStatus = w.result?.web_search_status
  const pendingProposals = w.result?.memory_proposals?.filter(item => pendingMemoryIds.includes(item.memory_id)) || []
  const canAdjust = w.plan && !waiting && !w.result?.quote_id && !w.pendingCommand
  const lastEvent = w.events[w.events.length - 1]

  return <div className={`workspace-grid ${showPanel ? 'panel-open' : ''}`}>
    <section className="conversation-area">
      <div className="conversation-toolbar"><div className="toolbar-title"><span className="assistant-symbol"><Sparkles size={15} /></span><span>车序助手</span><span className="caption">为每一步决策找到依据</span></div>
        <button className="icon-button panel-toggle" onClick={() => setShowPanel(!showPanel)} title="任务与资料" aria-label="切换任务与资料面板"><ListChecks size={18} /></button>
        {w.contextInfo?.digest && w.contextInfo.digest.covered_messages > 0 && <span className="context-tag" title={`已整理 ${w.contextInfo.digest.covered_messages} 条历史消息，原始记录仍保留`}>历史已整理</span>}
      </div>
      <div className={`conversation-scroll ${isWelcome ? 'welcome-scroll' : ''}`} ref={scrollArea} onScroll={() => {
        const node = scrollArea.current; if (node) setNearBottom(node.scrollHeight - node.scrollTop - node.clientHeight < 100)
      }}>
        {w.loading ? <div className="loading-center"><Spinner label="正在读取会话" /></div> : isWelcome ? <div className="welcome">
          <div className="welcome-hero"><div className="hero-copy"><span className="welcome-pill"><span className="gold-dot" />你的智能工作伙伴</span>
            <h1>让每一次决策，<br /><em>都有据可循。</em></h1><p>从找车、核对资料到协作报价，<br />把需求交给车序，让思路成为清晰的行动。</p></div><CarArtwork /></div>
          <div className="welcome-divider"><span>今天，从哪件事开始？</span><Compass size={15} /></div>
          <div className="suggestion-grid">{suggestions.map(item => <button className="suggestion" key={item.title} onClick={() => choose(item.text, item.skill)}><span className="suggestion-icon"><item.icon size={20} strokeWidth={1.5} /></span><span><strong>{item.title}</strong><small>{item.detail}</small></span><ArrowUp size={15} className="suggestion-arrow" /></button>)}</div>
          <button className="welcome-upload" onClick={onReports}><Plus size={14} />先上传一份车辆资料<ArrowRight size={14} /></button>
        </div> : <div className="message-list">{allVisible.length > visible.length && <button className="button subtle load-more" onClick={() => { setNearBottom(false); setVisibleCount(count => count + 40) }}>查看更早的消息</button>}{visible.map((message, index) => <article key={`${w.threadId}-${allVisible.length - visible.length + index}`} className={`message ${message.role}`}>
          {message.role === 'assistant' && <span className="message-avatar"><Sparkles size={16} /></span>}
          <div className="message-body">{message.role === 'assistant' && <span className="message-label">车序助手</span>}<div className={message.role === 'assistant' ? 'markdown' : 'user-text'}>
            {message.role === 'assistant' ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer noopener">{children}</a>, img: () => <span className="caption">[图片未加载，请核对来源]</span> }}>{String(message.content)}</ReactMarkdown> : String(message.content)}
          </div></div></article>)}
          {running && <div className="message assistant"><span className="message-avatar"><Sparkles size={16} /></span><div className="thinking"><div className="thinking-dots"><i /><i /><i /></div><span>{lastEvent ? eventName(lastEvent) : '任务已收到，准备开始'}</span></div></div>}
          {waiting && <div className="inline-notice warm"><ShieldCheck size={18} /><div><strong>报价正在等待经理审核</strong><p>审核结果会保留在这段会话中，确认前不会保存为成交报价。</p></div></div>}
          {failed && <div className="inline-notice bad"><RefreshCw size={19} /><div><strong>{w.task?.error_message || '这次执行暂未完成'}</strong><p>可以恢复未完成步骤，或结束本轮后缩小需求重试。</p><div className="button-row"><button className="button small" disabled={!!operation} onClick={() => perform('resume', w.resume)}>{operation === 'resume' ? <Spinner label="正在恢复" /> : '恢复执行'}</button><button className="text-button" disabled={!!operation} onClick={() => perform('cancel', w.cancel)}>结束本轮</button></div></div></div>}
          {!!pendingProposals.length && <button className="memory-reminder" onClick={onMemories}><Fingerprint size={18} /><span>有 {pendingProposals.length} 条偏好等待你确认<small>确认后才会用于后续任务</small></span><ChevronRight size={17} /></button>}
          <div ref={bottom} />
        </div>}
      </div>
      {!nearBottom && !isWelcome && <button className="scroll-bottom icon-button" aria-label="回到最新消息" onClick={() => { setNearBottom(true); bottom.current?.scrollIntoView({ behavior: 'smooth' }) }}><ArrowDown size={17} /></button>}
      <div className="composer-wrap"><ErrorText message={opError} />
        {w.pendingCommand && <p className="composer-state"><Spinner label="任务调整已提交，正在等待生效" /></p>}
        {running && <div className="composer-state"><span><i className={`connection-dot ${w.connected ? 'online' : ''}`} />{w.connected ? '正在同步执行进度' : '连接恢复中，任务仍在后台执行'}</span><button className="text-button" onClick={() => perform('cancel', w.cancel)} disabled={!!operation || w.task?.status === 'cancel_requested'}><Square size={11} />停止执行</button></div>}
        <form className={`composer ${running || waiting ? 'composer-disabled' : ''}`} onSubmit={event => { event.preventDefault(); void submit() }}>
          <textarea ref={composer} rows={2} value={draft} maxLength={2000} disabled={running || waiting || !!w.pendingCommand || failed} onChange={event => setDraft(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit() } }}
            placeholder={waiting ? '先完成这笔报价的审核，再继续对话' : failed ? '恢复或结束当前执行后，可以继续对话' : w.plan?.status === 'needs_input' ? '补充所需资料，继续当前计划…' : '告诉车序你的需求，例如：找一辆十万元以内的家用车…'} aria-label="输入你的需求" />
          <div className="search-shortcut"><button type="button" className="text-button" disabled={running || waiting} onClick={() => choose('请联网搜索最近一个月的二手车市场信息，列出来源和日期：', 'web_research')}><Search size={13} />联网查资料</button></div><div className="composer-bottom"><div className="composer-tools"><div className="skill-select"><Sparkles size={14} /><select aria-label="选择处理方式" value={skill} disabled={running || waiting} onChange={event => setSkill(event.target.value)}><option value="">智能选择</option>{skills.map(item => <option key={item.id} value={item.id}>{skillNames[item.id] || item.id}</option>)}</select><ChevronDown size={12} /></div><span className="composer-separator" /><button type="button" className="composer-tool" title="上传车辆资料" onClick={onReports}><Plus size={18} /></button><button type="button" className="composer-tool" title="查看已确认偏好" onClick={onMemories}><Fingerprint size={17} /></button></div>
            <button type="submit" className="send-button" disabled={!draft.trim() || running || waiting || failed || w.sending || !!w.pendingCommand || !!operation} aria-label="发送任务">{w.sending || operation === 'continue' ? <Spinner label="" /> : <ArrowUp size={20} />}</button></div>
        </form><p className="composer-footnote">车序可能会出错，请核对重要信息。报价需人工确认。<span>Enter 发送 · Shift + Enter 换行</span></p>
      </div>
    </section>

    {showPanel && <button className="panel-scrim" aria-label="关闭资料面板" onClick={() => setShowPanel(false)} />}
    <aside className="context-panel"><div className="panel-tabs"><button className={panel === 'progress' ? 'active' : ''} onClick={() => setPanel('progress')}><Waypoints size={16} />任务与结果</button><button className={panel === 'sources' ? 'active' : ''} onClick={() => setPanel('sources')}><BookOpen size={15} />资料来源{(analysis || webSources.length > 0) && <i className="gold-dot" />}</button><button className="panel-close icon-button" aria-label="关闭资料面板" onClick={() => setShowPanel(false)}><X size={16} /></button></div>
      <div className="panel-scroll">{panel === 'progress' ? <>
        <div className="panel-heading"><h2>任务进度</h2>{w.task ? <Status value={w.plan?.status === 'needs_input' ? 'needs_input' : w.task.status} /> : <span className="caption">准备就绪</span>}</div>
        {!w.task && !w.plan ? <div className="panel-welcome"><div className="orbit-icon"><Waypoints size={29} strokeWidth={1.1} /><span /><i /></div><h3>每一步，都清晰可见</h3><p>任务开始后，你可以在这里查看<br />处理进度、资料依据和执行结果。</p><div className="capability-list"><span><Search size={16} />查询真实库存<CheckDot /></span><span><FileText size={16} />回溯报告原文<CheckDot /></span><span><ShieldCheck size={16} />确认后再执行<CheckDot /></span></div></div> : <>
          {w.plan ? <div className="plan-steps">{w.plan.steps.map((step, index) => <div key={step.id} className={`plan-step ${step.status}`}><div className="step-line"><StepIcon status={step.status} /></div><div><span className="step-number">步骤 {String(index + 1).padStart(2, '0')} · {skillNames[step.skill_id] || '任务处理'}</span><p>{step.goal}</p><span className="caption">{({ completed: '已完成', running: '正在处理', pending: '等待执行', needs_input: '需要补充资料', blocked: '资料不足', skipped: '已跳过' } as Record<string, string>)[step.status] || step.status}</span></div></div>)}</div> : <div className="simple-progress"><span className={`step-dot ${running ? 'pulse' : 'finished'}`} /><div><strong>{running ? lastEvent ? eventName(lastEvent) : '正在处理需求' : waiting ? '等待人工审核' : '本轮处理已结束'}</strong><p>{w.task?.skill_id ? skillNames[w.task.skill_id] : '智能处理'} · {w.task && when(w.task.created_at)}</p></div></div>}
          {canAdjust && <button className="button full subtle" onClick={() => { setAdjustMode(w.plan?.status === 'needs_input' ? 'continue' : 'revise'); setAdjustText(''); setAdjusting(true) }}><SlidersHorizontal size={14} />{running ? '调整剩余计划' : '补充或调整任务'}</button>}
          {!!w.plan?.adjustments.length && <details className="details"><summary>查看调整记录<ChevronDown size={13} /></summary>{w.plan.adjustments.map((item, index) => <p key={index}>{item}</p>)}</details>}
        </>}
        {(vehicles.length > 0 || w.result?.quote_draft) && <div className="result-section"><div className="panel-heading"><h2>本次结果</h2><span className="caption">依据当前查询</span></div>{vehicles.map(vehicle => <VehicleCard key={vehicle.vehicle_id} vehicle={vehicle} onChoose={choose} />)}{w.result?.quote_draft && <QuoteCard quote={w.result.quote_draft} saved={!!w.result.quote_id} />}</div>}
        {!!w.events.length && <details className="details execution-details"><summary>执行记录<span>{w.events.length}</span><ChevronDown size={13} /></summary><div className="event-list">{w.events.slice(-12).reverse().map(event => <div key={event.sequence}><i /><span>{eventName(event)}</span><time>{new Date(event.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</time></div>)}</div></details>}
        <div className="panel-bottom-note"><ShieldCheck size={16} /><p>库存信息以本轮查询为准。<br />关键操作保留人工确认。</p></div>
      </> : <><div className="panel-heading"><h2>资料与来源</h2><span className="caption">可回溯</span></div>{(webStatus || webSources.length > 0) && <section className="web-sources"><h3>联网来源 · Tavily</h3><p className="caption">网页信息仅供参考；公开挂牌价不代表成交价或门店报价。</p>{webStatus === 'failed' && <p className="caption">本轮搜索失败，请查看回复中的原因。</p>}{webStatus === 'empty' && <p className="caption">未取得可用来源，请调整搜索条件。</p>}{webStatus === 'partial' && <p className="caption">部分查询失败，以下只展示成功取得的来源。</p>}{webSources.map(source => <article key={source.url}><a href={source.url} target="_blank" rel="noopener noreferrer"><span>{source.title}</span><ArrowUp size={13} /></a><span className="web-domain">{new URL(source.url).hostname}</span><p>{source.snippet}</p><span className="caption">获取于 {when(source.retrieved_at)}{source.published_date ? ` · 页面日期 ${source.published_date}` : ' · 发布时间未知'}</span></article>)}</section>}{analysis ? <div className="findings">{([['报告事实', analysis.facts], ['未知信息', analysis.unknowns], ['建议复检', analysis.follow_ups]] as const).map(([title, findings]) => findings.length > 0 && <section key={title}><h3>{title}</h3>{findings.map((finding, index) => <article key={index}><p>{finding.statement}</p>{finding.evidence.map((reference, refIndex) => <button key={refIndex} className="citation" onClick={() => onReference(reference)}><FileText size={13} /><span>{reference.filename} · 片段 {reference.chunk}</span><ArrowUp size={12} /></button>)}</article>)}</section>)}<p className="caption">已核对引用摘录，原文对结论的支持关系仍需人工复核。</p></div> : !webStatus && !webSources.length && <Empty title="让结论回到原文">读取检测报告后，相关引用会出现在这里。<button className="text-button" onClick={onReports}>上传或查看资料<ArrowRight size={14} /></button></Empty>}</>}
      </div><div className="panel-footer"><span className="gold-dot" />资料有出处 · 操作有记录</div>
    </aside>
    {adjusting && <Modal title="调整任务计划" description="当前批次执行结束后生效；已完成步骤是否保留，由调整方式决定。" onClose={() => !operation && setAdjusting(false)}><form onSubmit={event => { event.preventDefault(); void perform('adjust', async () => { await w.adjust(adjustMode, adjustText.trim()); setAdjusting(false) }) }}>
      <label>调整方式<select value={adjustMode} onChange={event => setAdjustMode(event.target.value)}><option value="revise">修改目标或条件</option><option value="replan">保留已完成步骤，重排剩余工作</option>{w.plan?.status === 'needs_input' && <option value="continue">补充信息并继续</option>}</select></label>
      <label>补充说明<textarea required maxLength={2000} value={adjustText} onChange={event => setAdjustText(event.target.value)} placeholder="例如：预算调整为12万元，保留已完成的库存查询…" rows={4} /></label><ErrorText message={opError} /><div className="modal-actions"><button type="button" className="button subtle" disabled={!!operation} onClick={() => setAdjusting(false)}>取消</button><button className="button primary" disabled={!!operation || !adjustText.trim()}>{operation ? <Spinner /> : '提交调整'}</button></div>
    </form></Modal>}
  </div>
}

function CheckDot() { return <span className="check-dot">✓</span> }
