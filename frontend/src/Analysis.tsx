import { useEffect, useState } from 'react';
import { request, purposeLabel, type Asset, type Suggestion, type DesignAnalysis } from './api';

type Configuration = { configured: boolean; status: string; reason?: string; request_limit: number | null; unlimited?: boolean; used: number; backend?: string; model?: string };
type Job = { id: string; status: string; reason?: string; suggestion?: Suggestion };
const designLabels: Record<keyof DesignAnalysis, string> = { palette: '配色', typography: '字体', composition: '构图与版式', hierarchy: '信息层级', imagery: '视觉手法', reusable_ideas: '可借鉴的方法', applicability: '适用场景', limitations: '证据与不确定项' };

export default function Analysis({ asset, canEdit, onAsset, onApply }: { asset: Asset; canEdit: boolean; onAsset: (a: Asset) => void; onApply: (s: Suggestion, jobId: string) => void }) {
 const [config, setConfig] = useState<Configuration | null>(null), [jobs, setJobs] = useState<Job[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0), [applied, setApplied] = useState(false);
 const running = jobs.some(job => ['queued', 'running', 'waiting_remote'].includes(job.status));
 useEffect(() => {
  let alive = true;
  Promise.all([request<Configuration>('/analysis/config'), request<{ items: Job[] }>(`/assets/${encodeURIComponent(asset.id)}/analysis`)]).then(([c, j]) => { if (alive) { setConfig(c); setJobs(j.items); setError(''); } }).catch(e => { if (alive) setError(e.message); });
  return () => { alive = false; };
 }, [asset.id, refresh]);
 useEffect(() => {
  if (!running) return;
  const timer = setTimeout(async () => {
   try { const latest = await request<Asset>(`/assets/${encodeURIComponent(asset.id)}`); onAsset(latest); } catch (e) { setError((e as Error).message); }
   setRefresh(n => n + 1);
  }, 2500);
  return () => clearTimeout(timer);
 }, [running, refresh, asset.id, onAsset]);
 const suggestion = asset.analysis_suggestion;
 const fresh = !!suggestion && asset.analysis_suggestion_revision === asset.revision;
 const allowed = canEdit && asset.permissions.analysis === 'allowed' && config?.status === 'ready' && !busy && !running;
 const retry = !!jobs[0] && (['failed', 'interrupted'].includes(jobs[0].status) || config?.backend === 'nova' && jobs[0].status === 'completed');
 async function analyze() {
  setBusy(true); setError(''); setApplied(false);
  try { const job = await request<Job>(`/assets/${encodeURIComponent(asset.id)}/analysis`, 'POST', { expected_revision: asset.revision, retry }); setJobs(previous => [job, ...previous.filter(j => j.id !== job.id)]); setRefresh(n => n + 1); }
  catch (e) { setError((e as Error).message); }
  finally { setBusy(false); }
 }
 return <details className="detail-section analysis-section"><summary>分析建议 <span className="muted">{running ? '处理中' : suggestion ? '待人工复核' : config?.configured ? '按需分析' : '未配置模型'}</span></summary>
  <p className="muted">仅分析这张图片和必要说明。建议不会自动改变收录状态、用途权限或发布结果。</p>
  {config?.backend === 'nova' && <p className="muted">通过 Nova 的视觉理解服务分析；实际模型以本次分析结果为准。</p>}
  {config && <p className="analysis-status">{config.reason}<br />{config.request_limit === null ? <>无每日次数上限 · 今日已调用 {config.used} 次 · 按 UTC 日统计</> : <>今日模型请求 {config.used} / {config.request_limit} · 按 UTC 日重置</>}</p>}
  {asset.permissions.analysis !== 'allowed' && <p className="analysis-status">这张素材的模型分析权限尚未允许。管理员核对依据并保存权限后，才能提交分析。</p>}
  {!canEdit && <p className="muted">发起分析需要编辑权限。</p>}
  {error && <p className="analysis-error" role="alert">{error}</p>}
  {jobs[0]?.reason && <p className="muted">最近结果：{jobs[0].reason}</p>}
  <button className="button secondary small" type="button" disabled={!allowed} onClick={analyze}>{running ? '正在分析…' : busy ? '正在提交…' : retry ? jobs[0]?.status === 'completed' ? '重新分析（计入额度）' : '确认重试分析（计入额度）' : '分析这张图片'}</button>
  {suggestion && <div className="suggestion"><span className="eyebrow">模型建议 · 尚未审核</span>
   {suggestion.model_version && <p className="muted">本次模型：{suggestion.model_version}</p>}
   <p><strong>{{ advertisement: '广告素材', reference: '设计参考', uncertain: '尚待判断' }[suggestion.kind] || suggestion.kind}</strong>{suggestion.purposes.map(p => <span className={`tag ${p}`} key={p}>{purposeLabel[p]}</span>)}</p><p>{suggestion.reason}</p>{suggestion.description && <p className="muted">{suggestion.description}</p>}
   {suggestion.design && <dl>{(Object.keys(designLabels) as Array<keyof DesignAnalysis>).map(key => { const field = suggestion.design?.[key]; return <div key={key}><dt><strong>{designLabels[key]}</strong></dt><dd style={{ margin: '6px 0 16px', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{(typeof field === 'object' ? field?.text : field) || '暂无可靠结论'}</dd></div>; })}</dl>}
   {!fresh && <p className="analysis-error">素材已修改，这份建议已过期。请重新分析。</p>}{fresh && !asset.analysis_suggestion_job_id && <p className="analysis-error">这份建议缺少与当前版本匹配的有效任务记录。请重新分析，或在人工审阅区独立填写。</p>}<button className="button secondary small" disabled={!canEdit || !fresh || !asset.analysis_suggestion_job_id} onClick={() => { if (!asset.analysis_suggestion_job_id) return; onApply(suggestion, asset.analysis_suggestion_job_id); setApplied(true); }}>采用建议填写</button>{applied && <p className="analysis-status" role="status">已填入人工审阅草稿，尚未完成审核。请逐项核对摘要和八项设计分析，勾选确认后再保存。</p>}</div>}
 </details>;
}
