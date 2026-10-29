import { useEffect, useId, useRef } from 'react'
import { ArrowUpRight, Check, CheckCircle2, Circle, CircleAlert, LoaderCircle, X } from 'lucide-react'
import type { PropsWithChildren } from 'react'
import type { Quote, TaskEvent, Vehicle } from './types'

export const money = (value: number) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value)
export const when = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
export const statusNames: Record<string, string> = { queued: '排队中', running: '执行中', cancel_requested: '正在停止', completed: '已完成', awaiting_approval: '等待审核', cancelled: '已取消', failed: '执行失败', timed_out: '已超时', interrupted: '执行中断', needs_input: '等待补充', pending: '待处理', approved: '已批准', rejected: '已拒绝', expired: '已过期', confirmed: '已确认', skipped: '已跳过', blocked: '资料不足' }
export const skillNames: Record<string, string> = { inventory_match: '库存匹配', vehicle_compare: '车辆对比', quote_assist: '报价协作', vehicle_report: '报告解读', deal_review: '任务协作', user_memory: '偏好记忆', web_research: '联网检索' }
const toolNames: Record<string, string> = { get_vehicle: '查询车辆资料', search_vehicles: '筛选门店库存', create_quote_draft: '计算报价预览', request_quote_approval: '提交经理审批', list_vehicle_reports: '查找检测报告', read_vehicle_report: '读取报告原文', finalize_vehicle_report: '核对原文引用', propose_user_memory: '整理待确认偏好' }

export function eventName(item: TaskEvent) {
  if (item.event === 'tool.start') return toolNames[String(item.data.tool_name)] || '处理资料'
  if (item.event === 'tool.end') return `${toolNames[String(item.data.tool_name)] || '资料处理'}完成`
  if (item.event.includes('error') || item.event.endsWith('failed')) return '这一步暂未完成'
  return ({ 'task.queued': '任务已加入队列', 'run.start': '开始处理你的任务', 'model.start': '正在分析下一步', 'model.end': '已整理处理思路', 'run.end': '本轮执行结束', 'plan.created': '任务计划已就绪', 'plan.replanned': '已调整剩余计划', 'subagent.started': '开始处理独立步骤', 'subagent.completed': '独立步骤已完成', 'task.completed': '任务已完成', 'task.awaiting_approval': '等待经理审核', 'task.cancelled': '任务已停止', 'context.compacted': '已整理历史上下文', 'report.analysis.validated': '报告引用已核对', 'plan.action.applied': '任务调整已生效' } as Record<string, string>)[item.event] || '更新任务状态'
}

export function Brand({ compact = false }: { compact?: boolean }) {
  return <div className="brand"><span className="brand-mark"><svg viewBox="0 0 48 48" aria-hidden="true"><path d="M10 16h28M10 24h28M10 32h28M19 9v30M29 9v30" /></svg></span>{!compact && <span className="brand-name">车序<span>CHEXU</span></span>}</div>
}
export function Spinner({ label = '处理中' }: { label?: string }) { return <span className="spinner-label"><LoaderCircle size={15} className="spin" />{label}</span> }
export function Status({ value }: { value: string }) {
  const tone = ['completed', 'confirmed', 'approved'].includes(value) ? 'good' : ['failed', 'timed_out', 'rejected', 'expired'].includes(value) ? 'bad' : ['running', 'queued', 'awaiting_approval', 'pending', 'needs_input'].includes(value) ? 'warm' : 'muted'
  return <span className={`status ${tone}`}><i />{statusNames[value] || value}</span>
}
export function Empty({ title, children }: PropsWithChildren<{ title: string }>) {
  return <div className="empty"><Circle size={28} strokeWidth={1} /><h3>{title}</h3><p>{children}</p></div>
}
export function ErrorText({ message }: { message: string }) { return message ? <p className="error-inline" role="alert"><CircleAlert size={16} />{message}</p> : null }

export function Modal({ title, description, children, onClose }: PropsWithChildren<{ title: string; description?: string; onClose: () => void }>) {
  const dialog = useRef<HTMLDialogElement>(null)
  const headingId = useId()
  useEffect(() => {
    const node = dialog.current
    node?.showModal()
    return () => { node?.close() }
  }, [])
  return <dialog ref={dialog} className="modal" aria-labelledby={headingId} onCancel={event => { event.preventDefault(); onClose() }} onClick={event => { if (event.target === event.currentTarget) onClose() }}>
    <div className="modal-inner"><header><div><h2 id={headingId}>{title}</h2>{description && <p>{description}</p>}</div><button className="icon-button" onClick={onClose} aria-label="关闭对话框"><X size={19} /></button></header>{children}</div>
  </dialog>
}

export function VehicleCard({ vehicle, onChoose }: { vehicle: Vehicle; onChoose?: (value: string) => void }) {
  return <article className="vehicle-card"><div className="vehicle-card-top"><span className="mono">{vehicle.vehicle_id}</span>{vehicle.is_demo && <span className="tiny-tag">模拟资料</span>}</div>
    <h3>{vehicle.title}</h3><div className="vehicle-numbers"><strong><span>¥</span>{money(vehicle.price_cny)}</strong><span>{money(vehicle.mileage_km)} <small>公里</small></span></div>
    {onChoose && <button className="text-button" onClick={() => onChoose(`为 ${vehicle.vehicle_id} 生成报价预览，不提交审批。`)}>生成报价预览<ArrowUpRight size={14} /></button>}
  </article>
}

export function QuoteCard({ quote, saved = false }: { quote: Quote; saved?: boolean }) {
  return <article className="quote-card"><div className="section-heading"><span>车辆报价</span><span className="tiny-tag">{saved ? '已保存' : '预览'}</span></div>
    <h3>{quote.vehicle_title}</h3><span className="mono caption">{quote.vehicle_id}</span>
    <dl><div><dt>库存售价</dt><dd>¥ {money(quote.listed_price_cny)}</dd></div><div><dt>优惠金额</dt><dd>− ¥ {money(quote.discount_cny)}</dd></div><div className="quote-total"><dt>车辆报价</dt><dd>¥ {money(quote.quoted_price_cny)}</dd></div></dl>
    <p className="caption"><CircleAlert size={13} />不含待确认费用：{quote.pending_items.join('、')}。</p>
  </article>
}

export function StepIcon({ status }: { status: string }) {
  return status === 'completed' ? <CheckCircle2 size={20} /> : status === 'running' ? <LoaderCircle size={20} className="spin" /> : ['blocked', 'failed', 'needs_input'].includes(status) ? <CircleAlert size={20} /> : <Circle size={20} />
}
export function CheckLine({ children }: PropsWithChildren) { return <span className="check-line"><Check size={14} />{children}</span> }
