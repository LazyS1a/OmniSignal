import { useCallback, useEffect, useRef, useState } from 'react';

type PilotState = 'disabled' | 'not_started' | 'blocked' | 'before_period' | 'weekend' | 'waiting_window' | 'running' | 'finished' | 'no_eligible' | 'expired_intent' | 'submission_uncertain' | 'ended';
type Policy = { start_date: string; end_date: string; timezone: string; weekdays: number[]; hour: number; minute: number; target: number; review_target_min: number; review_target_max: number };
type Report = { day: string; state: PilotState; batch_id: string | null; planned_images: number; eligible_at_plan: number; submitted: number; analysed_images: number; failed: number; queued: number; running: number; projects_covered: number; submitted_projects: number; skip_reasons: Record<string, number>; pending_at_plan: number; examined: number; scan_limited: boolean; submission_shortfall: number; analysis_shortfall: number; requires_human_review: number; review_target_min: number; review_target_max: number; reviewed_count: number | null; updated_at: string | number };
type Snapshot = { enabled: boolean; worker_enabled: boolean; state: PilotState; diagnostic: string | null; policy: Policy | null; current_day: string | null; report: Report | null; recent: Report[] };

const states: Record<PilotState, string> = {
 disabled: '自动计划已关闭', not_started: '今日尚未运行', blocked: '自动入队已暂停', before_period: '试点尚未开始',
 weekend: '周末不自动运行', waiting_window: '等待今日运行时间', running: '今日分析进行中', finished: '今日批次已结束',
 no_eligible: '暂无可分析素材', expired_intent: '今日计划已过期', submission_uncertain: '提交结果待核对', ended: '试点已结束',
};
const diagnostics: Record<string, string> = {
 invalid_policy: '试点配置无效', invalid_state: '本地调度记录需要核查', invalid_clock: '服务器时间无效', batch_lookup_failed: '批次状态暂时无法核对',
 observed_batch_missing: '已提交批次记录缺失', submission_uncertain: '提交结果待确认', clock_before_saved_state: '服务器时间早于已有记录',
 policy_changed_after_intent: '当日计划生成后配置发生变化', batch_without_intent: '批次与当日计划尚未对应', pilot_lock_busy: '另一个调度进程正在处理', pilot_operation_failed: '调度运行异常',
};
const reasons: Record<string, string> = {
 permission_required: '分析权限待确认', already_analyzed: '已有分析建议', already_running: '已在分析队列', retry_required: '需单独确认重试',
 disabled: '分析功能未启用', not_configured: '模型尚未配置', budget_exhausted: '可用额度不足', queue_full: '分析队列已满',
 missing: '素材已不存在', revision_changed: '素材内容已更新', not_pending: '素材已离开待处理区',
};

function count(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value) && value >= 0; }
function isSnapshot(value: unknown): value is Snapshot {
 if (!value || typeof value !== 'object') return false;
 const v = value as Snapshot;
 if (typeof v.enabled !== 'boolean' || typeof v.worker_enabled !== 'boolean' || !Object.hasOwn(states, v.state)) return false;
 if (v.policy && (!['hour', 'minute', 'target', 'review_target_min', 'review_target_max'].every(key => count(v.policy?.[key as keyof Policy])) || !/^\d{4}-\d{2}-\d{2}$/.test(v.policy.end_date))) return false;
 if (v.report && (!['submitted', 'analysed_images', 'failed', 'queued', 'running', 'submission_shortfall', 'analysis_shortfall', 'requires_human_review'].every(key => count(v.report?.[key as keyof Report])) || !v.report.skip_reasons || typeof v.report.skip_reasons !== 'object')) return false;
 return true;
}
function day(value: string | null | undefined) { return value && /^\d{4}-\d{2}-\d{2}$/.test(value) ? `${Number(value.slice(5, 7))}月${Number(value.slice(8, 10))}日` : '日期待核对'; }

export default function DailyPilot({ revision }: { revision: number }) {
 const [snapshot, setSnapshot] = useState<Snapshot | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState(false);
 const active = useRef(false), sequence = useRef(0), pending = useRef<AbortController | null>(null);
 const load = useCallback(async () => {
  pending.current?.abort();
  const controller = new AbortController(), id = ++sequence.current;
  pending.current = controller; setBusy(true); setError(false);
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
   const response = await fetch('/workbench/api/analysis/daily-pilot', { method: 'GET', credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
   if (response.status === 401) window.dispatchEvent(new Event('omni:session-expired'));
   if (!response.ok) throw new Error('read_failed');
   const value: unknown = await response.json();
   if (!isSnapshot(value)) throw new Error('invalid_snapshot');
   if (active.current && id === sequence.current) setSnapshot(value);
  } catch {
   if (active.current && id === sequence.current) setError(true);
  } finally {
   clearTimeout(timer);
   if (active.current && id === sequence.current) { pending.current = null; setBusy(false); }
  }
 }, []);
 useEffect(() => {
  active.current = true; void load();
  const refresh = () => { if (document.visibilityState === 'visible' && !pending.current) void load(); };
  const timer = setInterval(refresh, 30000);
  document.addEventListener('visibilitychange', refresh);
  return () => { active.current = false; ++sequence.current; pending.current?.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', refresh); };
 }, [load, revision]);

 const policy = snapshot?.policy, report = snapshot?.report;
 const attention = !!snapshot?.diagnostic || ['blocked', 'expired_intent', 'submission_uncertain'].includes(snapshot?.state || '') || (!!report && report.failed > 0);
 const label = snapshot ? !snapshot.worker_enabled && snapshot.enabled && snapshot.state !== 'ended' ? '执行器未启动' : snapshot.state === 'finished' && !!report?.failed ? '今日批次已结束 · 有失败' : states[snapshot.state] : '正在读取状态';
 const skipped = report ? Object.entries(report.skip_reasons).filter(([, value]) => count(value) && value > 0) : [];
 const reviewMin = policy?.review_target_min ?? report?.review_target_min, reviewMax = policy?.review_target_max ?? report?.review_target_max;

 return <section className="daily-pilot" aria-labelledby="daily-pilot-title" aria-busy={busy}>
  <div className="daily-pilot-heading"><div><h2 id="daily-pilot-title">每日试运行</h2><p className="daily-pilot-schedule">{policy ? <>周一至周五 · 北京时间 {String(policy.hour).padStart(2, '0')}:{String(policy.minute).padStart(2, '0')} · 试点至 {day(policy.end_date)}</> : snapshot ? '自动分析计划尚未配置' : error ? '计划状态暂时无法读取' : '正在读取自动分析计划…'}</p></div><div className="daily-pilot-actions">{snapshot && <span className={`daily-pilot-state${attention ? ' attention' : ''}`}>{label}</span>}<button type="button" className="text-button" disabled={busy} onClick={() => void load()} aria-label="刷新每日试运行状态">{busy ? '刷新中…' : '刷新状态'}</button></div></div>
  {error && <p className="daily-pilot-warning" role="alert">每日试运行状态读取失败，请稍后刷新。{snapshot && '下方为上次读取的快照，可能已经过时。'}</p>}
  {!snapshot ? !error && <p className="daily-pilot-note" role="status">正在读取每日试运行状态…</p> : <>
   <div className="daily-pilot-target"><strong>{policy ? `每日自动目标 ${policy.target} 张` : '每日目标待配置'}</strong><span>{day(snapshot.current_day)}（北京时间）{!report && ' · 今日尚无运行记录'}</span></div>
   <dl className="daily-pilot-counts" aria-label="今日自动分析统计">{[['已入队', report?.submitted], ['分析完成', report?.analysed_images], ['排队中', report?.queued], ['失败', report?.failed]].map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{typeof value === 'number' ? value : '—'}</dd></div>)}</dl>
   {report && report.running > 0 && <p className="daily-pilot-note">正在分析 {report.running} 张，完成数量会继续更新。</p>}
   {snapshot.diagnostic && <p className="daily-pilot-warning" role="status">{diagnostics[snapshot.diagnostic] || '调度状态需要核查'}。已暂停自动入队，请管理员核查。</p>}
   {!snapshot.worker_enabled && snapshot.enabled && snapshot.state !== 'ended' && <p className="daily-pilot-warning">执行器未启动，当前不会自动入队；请联系管理员检查服务。</p>}
   {snapshot.state === 'no_eligible' && <p className="daily-pilot-note">本次没有符合条件的新素材，未新增分析任务。</p>}
   {snapshot.state === 'expired_intent' && <p className="daily-pilot-note">今日计划已过期，不会自动补交；请管理员核对当日记录。</p>}
   {snapshot.state === 'ended' && <p className="daily-pilot-note">试点期间已结束，后续不会继续自动入队。</p>}
   {report && (report.submission_shortfall > 0 || report.analysis_shortfall > 0) && <div className="daily-pilot-shortfall"><p>{report.submission_shortfall > 0 && `今日入队比目标少 ${report.submission_shortfall} 张。`}{report.analysis_shortfall > 0 && ` 分析完成距目标还差 ${report.analysis_shortfall} 张。`}</p>{skipped.length ? <p className="daily-pilot-note">{skipped.map(([code, value]) => `${reasons[code] || '其他条件未满足'} ${value} 张`).join('；')}。</p> : <p className="daily-pilot-note">{report.queued + report.running > 0 ? '部分任务仍在队列中，请查看后续结果。' : report.failed > 0 ? '部分分析未完成，请在对应素材中查看结果。' : '当前符合条件的素材不足，实际处理量以当日报告为准。'}</p>}{report.scan_limited && <p className="daily-pilot-note">本次只检查了部分待处理素材。</p>}</div>}
   <p className="daily-pilot-review">{typeof reviewMin === 'number' && typeof reviewMax === 'number' ? `人工审核目标 ${reviewMin}–${reviewMax} 张 / 日` : '人工审核目标待配置'}<span> · 这是目标，人工完成量尚未统计。{report ? `当前待人工复核 ${report.requires_human_review} 张。` : '模型建议仍需人工确认。'}</span></p>
  </>}
 </section>;
}
