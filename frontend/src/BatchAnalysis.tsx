import { useEffect, useRef, useState } from 'react';
import { ApiError, request, type Asset } from './api';

type BatchInput = { asset_id: string; expected_revision: number };
type BatchItem = BatchInput & { title: string; disposition: 'eligible' | 'skipped' | 'submitted'; reason_code: string; reason: string; job_id: string | null; status: string | null; charged: boolean };
type Batch = {
 batch_id: string | null; request_id: string | null; state: 'preview' | 'running' | 'finished'; created_at: string | null;
 items: BatchItem[];
 totals: { requested: number; eligible: number; submitted: number; skipped: number; queued: number; running: number; completed: number; failed: number };
 quota: { limit: number; used: number; reserved: number; remaining: number; queue_remaining: number };
 provider: { configured: boolean; backend: string; model: string | null };
};
type Submission = { request_id: string; items: BatchInput[] };
const jobLabels: Record<string, string> = { queued: '排队中', running: '分析中', waiting_remote: '等待分析结果', completed: '分析完成 · 待复核', failed: '分析失败', interrupted: '任务中断', stale: '素材已更新' };

function savedId(key: string) { try { const value = sessionStorage.getItem(key); return value && /^[a-f0-9]{32}$/.test(value) ? value : ''; } catch { return ''; } }
function remember(key: string, value: string) { try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); } catch { /* Progress remains available in this page when browser storage is disabled. */ } }

export default function BatchAnalysis({ actor, assets, loading, selected, onSelection, onAsset, onBusy, onSubmitted }: {
 actor: string; assets: Asset[]; loading: boolean; selected: string[]; onSelection: (ids: string[]) => void; onAsset: (asset: Asset) => void; onBusy: (busy: boolean) => void; onSubmitted: () => void;
}) {
 const storageKey = `omni:analysis-batch:${encodeURIComponent(actor)}`;
 const [batchId, setBatchId] = useState(() => savedId(storageKey));
 const [batch, setBatch] = useState<Batch | null>(null), [preview, setPreview] = useState<Batch | null>(null);
 const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState('');
 const [uncertain, setUncertain] = useState(false), [poll, setPoll] = useState(0);
 const submission = useRef<Submission | null>(null), inFlight = useRef(false), generation = useRef(0), phase = useRef<'preview' | 'submit' | null>(null);
 const selectedAssets = assets.filter(asset => selected.includes(asset.id));
 const selectionKey = selectedAssets.map(asset => `${asset.id}:${asset.revision}`).join('|');
 const allSelected = assets.length > 0 && assets.every(asset => selected.includes(asset.id));
 const inspecting = phase.current === 'preview';
 const result = inspecting ? null : preview || batch;
 const active = batch?.state === 'running';

 useEffect(() => { onBusy(busy || uncertain); return () => onBusy(false); }, [busy, uncertain, onBusy]);

 useEffect(() => {
  if (phase.current === 'submit' || uncertain) return;
  if (phase.current === 'preview' || preview) setOpen(false);
  generation.current += 1; setPreview(null); submission.current = null; setError('');
 }, [selectionKey, uncertain]);

 useEffect(() => {
  if (!batchId || preview) return;
  let alive = true;
  const timer = setTimeout(async () => {
   try {
    const value = await request<Batch>(`/analysis/batches/${batchId}`);
    if (alive) { setBatch(value); setUncertain(false); setError(''); }
   } catch (e) {
    if (alive) setError(e instanceof ApiError && e.status === 404 ? '暂未查到这一批的记录。可再次查询；提交结果不明时，请勿反复新建批次。' : (e as Error).message);
   }
  }, batch ? 2500 : 0);
  return () => { alive = false; clearTimeout(timer); };
 }, [batchId, poll, preview]);

 useEffect(() => {
  if (!active || preview) return;
  const timer = setTimeout(() => setPoll(n => n + 1), 3000);
  return () => clearTimeout(timer);
 }, [active, batch, poll, preview]);

 async function inspect() {
  if (inFlight.current || !selectedAssets.length || uncertain) return;
  inFlight.current = true; phase.current = 'preview'; setBusy(true); setOpen(true); setError(''); setPreview(null);
  const version = ++generation.current;
  const items = selectedAssets.map(asset => ({ asset_id: asset.id, expected_revision: asset.revision }));
  submission.current = { request_id: crypto.randomUUID(), items };
  try {
   const value = await request<Batch>('/analysis/batches/preview', 'POST', { items });
   if (version === generation.current) setPreview(value);
  } catch (e) { if (version === generation.current) setError((e as Error).message); }
  finally { inFlight.current = false; phase.current = null; setBusy(false); }
 }

 async function submit() {
  const payload = submission.current;
  if (inFlight.current || !payload || (!uncertain && !preview?.totals.eligible)) return;
  inFlight.current = true; phase.current = 'submit'; setBusy(true); setError('');
  const id = payload.request_id.replaceAll('-', '').toLowerCase();
  remember(storageKey, id);
  try {
   const value = await request<Batch>('/analysis/batches', 'POST', payload);
   setBatch(value); setBatchId(id); setPreview(null); setUncertain(false); submission.current = null; onSubmitted();
  } catch (e) {
   // A lost POST response is ambiguous. Recover by GET first and retain the
   // exact request ID/payload for an explicit idempotent retry, never a new job.
   if (!(e instanceof ApiError) || e.status >= 500) {
    setUncertain(true); setBatchId(id);
    try {
     const value = await request<Batch>(`/analysis/batches/${id}`);
     setBatch(value); setPreview(null); setUncertain(false); submission.current = null; onSubmitted();
    } catch { setError('提交结果暂未确认。可用同一请求安全重试，或稍后查询；不会自动重新发起分析。'); }
   } else { remember(storageKey, batch?.batch_id || ''); setError((e as Error).message); }
  } finally { inFlight.current = false; phase.current = null; setBusy(false); }
 }

 async function viewAsset(id: string) {
  try { onAsset(await request<Asset>(`/assets/${encodeURIComponent(id)}`)); }
  catch (e) { setError((e as Error).message); }
 }

 function showProgress() { setPreview(null); setOpen(true); setError(''); setPoll(n => n + 1); }
 function closePreview() { if (busy || uncertain) return; setPreview(null); submission.current = null; setOpen(false); setError(''); }

 return <section className="batch-analysis" aria-label="批量分析">
  <div className="batch-toolbar">
   <label className="batch-select-all"><input type="checkbox" checked={allSelected} disabled={loading || !assets.length || busy || uncertain} onChange={() => onSelection(allSelected ? [] : assets.map(asset => asset.id))} />全选本页</label>
   <span className="muted">已选 {selected.length} 张</span>
   {selected.length > 0 && <button className="text-button" disabled={busy || uncertain} onClick={() => onSelection([])}>取消选择</button>}
   <div className="batch-toolbar-actions">
    {batchId && <button className="text-button" onClick={showProgress} disabled={busy || uncertain}>查看最近批次{active ? ' · 处理中' : ''}</button>}
    <button className="button secondary small" disabled={loading || !selected.length || busy || uncertain} onClick={inspect}>{busy && !preview ? '正在检查…' : '批量分析'}</button>
   </div>
  </div>
  <p className="batch-hint">先检查可分析数量，再排队处理。已分析的图片会跳过，结果仍需人工复核。</p>
  {open && <section className="batch-panel" aria-labelledby="batch-heading">
   <div className="batch-heading"><h2 id="batch-heading">{preview || inspecting ? '确认批量分析' : '批量分析进度'}</h2><button className="text-button" disabled={busy || uncertain} onClick={closePreview}>收起</button></div>
   {error && <p className="analysis-error" role="alert">{error}</p>}
   {!result && !error && <p className="muted" role="status">{inspecting ? '正在检查所选图片…' : '正在读取批次信息…'}</p>}
   {result && <>
    <div className="batch-counts" aria-live="polite">
     {preview ? <><span><strong>{result.totals.eligible}</strong> 可排队</span><span><strong>{result.totals.skipped}</strong> 将跳过</span></> : <><span><strong>{result.totals.completed}</strong> 已完成</span><span><strong>{result.totals.queued + result.totals.running}</strong> 处理中</span><span><strong>{result.totals.failed}</strong> 未完成</span><span><strong>{result.totals.skipped}</strong> 已跳过</span></>}
    </div>
    {preview ? <p className="batch-hint">今日限额 {result.quota.limit} 次 · 已用 {result.quota.used} · 已排队预留 {result.quota.reserved} · 本次检查时剩余 {result.quota.remaining} 次。按 UTC 日重置，每张最多提交一次请求，失败也可能计入额度。提交时会再次核验。</p> : <p className="batch-hint">这一批提交了 {result.totals.submitted} 张。关闭页面不会取消任务；完成后打开图片详情查看分析建议。失败项可在详情中确认后重试。</p>}
    <p className="batch-hint">{result.provider.backend === 'nova' ? '沿用 Nova 后台当前模型，具体型号以分析结果为准。' : result.provider.model ? `分析模型：${result.provider.model}` : '模型尚未就绪。'}{preview && ' 批量分析不会改变图片用途权限或自动收录、发布。'}</p>
    <ul className="batch-items">{result.items.map(item => <li key={item.asset_id}>
     <div className="batch-item-main"><button className="text-button batch-item-title" onClick={() => viewAsset(item.asset_id)}>{item.title || '素材暂不可用'}</button><span className={`batch-state ${item.disposition === 'skipped' ? 'skipped' : item.status === 'completed' ? 'complete' : ''}`}>{item.disposition === 'eligible' ? '可排队' : item.disposition === 'skipped' ? '跳过' : jobLabels[item.status || ''] || '等待更新'}</span></div>
     {item.reason && <p className="muted">{item.reason}</p>}
    </li>)}</ul>
    {preview && <div className="form-actions"><button className="button secondary" disabled={busy || uncertain} onClick={closePreview}>暂不分析</button><button className="button primary" disabled={busy || (!uncertain && !preview.totals.eligible)} onClick={submit}>{busy ? '正在提交…' : uncertain ? '用同一请求安全重试' : `开始批量分析（${preview.totals.eligible} 张）`}</button></div>}
   </>}
   {!preview && error && <button className="button secondary small" disabled={busy} onClick={() => setPoll(n => n + 1)}>再次查询批次</button>}
  </section>}
 </section>;
}
