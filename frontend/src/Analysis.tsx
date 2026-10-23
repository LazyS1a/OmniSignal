import { useEffect, useState } from 'react';
import { request, purposeLabel, type Asset, type Suggestion } from './api';

type Configuration = { configured: boolean; status: string; reason?: string; request_limit: number; used: number };
type Job = { id: string; status: string; reason?: string; suggestion?: Suggestion };

export default function Analysis({ asset, canEdit, onAsset, onApply }: { asset: Asset; canEdit: boolean; onAsset: (a: Asset) => void; onApply: (s: Suggestion) => void }) {
 const [config, setConfig] = useState<Configuration | null>(null), [jobs, setJobs] = useState<Job[]>([]), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0), [applied, setApplied] = useState(false);
 const running = jobs.some(job => ['queued', 'running'].includes(job.status));
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
 const retry = !!jobs[0] && ['failed', 'interrupted'].includes(jobs[0].status);
 async function analyze() {
  setBusy(true); setError(''); setApplied(false);
  try { const job = await request<Job>(`/assets/${encodeURIComponent(asset.id)}/analysis`, 'POST', { expected_revision: asset.revision, retry }); setJobs(previous => [job, ...previous.filter(j => j.id !== job.id)]); setRefresh(n => n + 1); }
  catch (e) { setError((e as Error).message); }
  finally { setBusy(false); }
 }
 return <details className="detail-section analysis-section"><summary>分析建议 <span className="muted">{running ? '处理中' : suggestion ? '待人工复核' : config?.configured ? '按需分析' : '未配置模型'}</span></summary>
  <p className="muted">仅分析这张图片和必要说明。建议不会自动改变收录状态、用途权限或发布结果。</p>
  {config && <p className="analysis-status">{config.reason}<br />今日模型请求 {config.used} / {config.request_limit} · 按 UTC 日重置</p>}
  {asset.permissions.analysis !== 'allowed' && <p className="analysis-status">这张素材的模型分析权限尚未允许。管理员核对依据并保存权限后，才能提交分析。</p>}
  {!canEdit && <p className="muted">发起分析需要编辑权限。</p>}
  {error && <p className="analysis-error" role="alert">{error}</p>}
  {jobs[0]?.reason && <p className="muted">最近结果：{jobs[0].reason}</p>}
  <button className="button secondary small" type="button" disabled={!allowed} onClick={analyze}>{running ? '正在分析…' : busy ? '正在提交…' : retry ? '确认重试分析（计入额度）' : '分析这张图片'}</button>
  {suggestion && <div className="suggestion"><span className="eyebrow">模型建议 · 尚未审核</span><p><strong>{{ advertisement: '广告素材', reference: '设计参考', uncertain: '尚待判断' }[suggestion.kind] || suggestion.kind}</strong>{suggestion.purposes.map(p => <span className={`tag ${p}`} key={p}>{purposeLabel[p]}</span>)}</p><p>{suggestion.reason}</p>{suggestion.description && <p className="muted">{suggestion.description}</p>}{!fresh && <p className="analysis-error">素材已修改，这份建议已过期。请重新分析。</p>}<button className="button secondary small" disabled={!canEdit || !fresh} onClick={() => { onApply(suggestion); setApplied(true); }}>采用建议填写</button>{applied && <p className="analysis-status" role="status">已填入表单。请核对内容，再选择收录或保留待处理。</p>}</div>}
 </details>;
}
