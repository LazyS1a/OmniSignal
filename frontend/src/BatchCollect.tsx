import { useCallback, useEffect, useRef, useState } from 'react';
import Modal from './Modal';
import { ApiError, request, safeUrl, purposeLabel, type Asset, type Purpose } from './api';

type Row = { asset: Asset; kind: string; purposes: Purpose[]; suggested: boolean };
type Receipt = { id: string; state: 'saved' | 'skipped' | 'failed' | 'unknown' | 'stopped'; message: string };
function initialRow(asset: Asset): Row {
 const suggestion = asset.analysis_suggestion_revision === asset.revision ? asset.analysis_suggestion : undefined;
 const suggested = !['advertisement', 'reference'].includes(asset.kind) && !!suggestion;
 return { asset, kind: suggested ? suggestion!.kind : asset.kind, purposes: [...(suggested ? suggestion!.purposes : asset.purposes)], suggested };
}
function skipReason(row: Row) {
 if (row.asset.publication_status === 'withdrawn') return '已撤回，不能重新收录';
 if (row.asset.status !== 'pending') return '已离开待处理区，请刷新';
 if (!['advertisement', 'reference'].includes(row.kind)) return '尚未确认内容类型';
 if (row.kind === 'advertisement' && !row.purposes.length) return '广告素材尚未选择用途';
 return '';
}

export default function BatchCollect({ assets, selected, disabled, onBusy, onDone }: {
 assets: Asset[]; selected: string[]; disabled: boolean; onBusy: (busy: boolean) => void; onDone: (message: string) => void;
}) {
 const [open, setOpen] = useState(false), [rows, setRows] = useState<Row[]>([]);
 const [reason, setReason] = useState(''), [receipts, setReceipts] = useState<Receipt[] | null>(null), [busy, setBusy] = useState(false);
 const inFlight = useRef(false), alive = useRef(true), snapshot = useRef('');
 const key = assets.filter(a => selected.includes(a.id)).map(a => `${a.id}:${a.revision}`).join('|');
 const stale = open && snapshot.current !== key;
 const eligible = rows.filter(row => !skipReason(row)).length;
 useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
 useEffect(() => { onBusy(open); return () => onBusy(false); }, [open, onBusy]);
 useEffect(() => {
  if (!busy) return;
  const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
  window.addEventListener('beforeunload', warn);
  return () => window.removeEventListener('beforeunload', warn);
 }, [busy]);
 function begin() {
  if (disabled || inFlight.current) return;
  snapshot.current = key;
  setRows(assets.filter(a => selected.includes(a.id)).map(initialRow));
  setReason(''); setReceipts(null); setOpen(true);
 }
 const close = useCallback(() => {
  if (inFlight.current) return;
  setOpen(false);
  if (receipts) onDone(`批量收录已结束：${receipts.filter(r => r.state === 'saved').length} 张成功，其余请查看待处理区。`);
 }, [receipts, onDone]);
 function edit(id: string, patch: Partial<Row>) { setRows(current => current.map(row => row.asset.id === id ? { ...row, ...patch, suggested: false } : row)); }
 async function submit() {
  if (inFlight.current || receipts || stale || !eligible || !reason.trim()) return;
  inFlight.current = true; setBusy(true); setReceipts([]);
  const result: Receipt[] = [];
  let stopped = false;
  for (const row of rows) {
   if (!alive.current) break;
   const id = row.asset.id, skip = skipReason(row);
   if (stopped) result.push({ id, state: 'stopped', message: '未提交，请刷新后重新选择' });
   else if (skip) result.push({ id, state: 'skipped', message: skip });
   else {
    try {
     const value = await request<Asset>(`/assets/${encodeURIComponent(id)}/review`, 'POST', {
      expected_revision: row.asset.revision, status: 'accepted', kind: row.kind,
      purposes: row.kind === 'reference' ? [] : row.purposes, reason: reason.trim(),
     });
     if (!value || value.id !== id || value.status !== 'accepted' || value.revision !== row.asset.revision + 1) throw new Error('Unexpected receipt');
     result.push({ id, state: 'saved', message: '已收录' });
    } catch (error) {
     const known = error instanceof ApiError && error.status >= 400 && error.status < 500;
     result.push({ id, state: known ? (error.status === 409 ? 'skipped' : 'failed') : 'unknown', message: known ? error.message : '结果待核实：请刷新查看此图状态；系统不会自动重发' });
     stopped = !known || (error instanceof ApiError && [401, 403, 429].includes(error.status));
    }
   }
   if (alive.current) setReceipts([...result]);
  }
  inFlight.current = false;
  if (alive.current) setBusy(false);
 }
 return <>
  <button className="button primary small" disabled={disabled || !selected.length} onClick={begin}>批量收录</button>
  {open && <Modal title={receipts ? '批量收录结果' : '确认批量收录'} onClose={close} wide>
   <div className="modal-body batch-collect">
    <p>已选 {rows.length} 张 · {eligible} 张可收录。可逐项调整分类，也可以统一设为同一种类型。</p>
    <p className="muted">本次只确认收录与分类，不会调用模型、修改用途权限或发布到下游。设计分析与发布分类仍需在详情中核对；已有审核会随版本更新而过期。</p>
    {!receipts && <>
     <label>统一内容类型<select aria-label="统一内容类型" defaultValue="" onChange={e => {
      const kind = e.target.value;
      setRows(current => current.map(row => kind ? { ...row, kind, purposes: kind === 'reference' ? [] : row.purposes, suggested: false } : initialRow(row.asset)));
     }}><option value="">保留逐项分类</option><option value="reference">全部设为设计参考</option><option value="advertisement">全部设为广告素材</option></select></label>
     <label>本批收录理由<textarea aria-label="本批收录理由" value={reason} onChange={e => setReason(e.target.value)} rows={2} maxLength={2000} placeholder="说明这批图片值得保留的参考价值" /></label>
    </>}
    {stale && !receipts && <p className="analysis-error" role="alert">列表已变化，请关闭面板后重新选择；本次尚未提交。</p>}
    {receipts && <p role="status">{busy ? '正在处理，请保持页面打开' : '本批处理结束'}：{receipts.filter(r => r.state === 'saved').length} 张成功，{receipts.filter(r => r.state === 'skipped').length} 张跳过，{receipts.filter(r => r.state === 'failed').length} 张失败，{receipts.filter(r => r.state === 'unknown').length} 张结果待核实，{receipts.filter(r => r.state === 'stopped').length} 张未提交。已处理 {receipts.length} / {rows.length}。</p>}
    <ul className="batch-collect-list">{rows.map(row => {
     const receipt = receipts?.find(r => r.id === row.asset.id), skip = skipReason(row);
     return <li key={row.asset.id}>
      {safeUrl(row.asset.thumbnail_url || row.asset.image_url) && <img src={safeUrl(row.asset.thumbnail_url || row.asset.image_url)} alt={row.asset.title} />}
      <div className="batch-collect-fields"><strong>{row.asset.title}</strong>
       {receipts ? <p className={receipt?.state === 'saved' ? 'positive' : 'muted'}>{receipt?.message || '等待处理'}</p> : <>
        <select aria-label={`内容类型 ${row.asset.title}`} value={row.kind} onChange={e => edit(row.asset.id, { kind: e.target.value, purposes: e.target.value === 'reference' ? [] : row.purposes })}><option value="uncertain">尚待判断</option><option value="advertisement">广告素材</option><option value="reference">设计参考</option></select>
        {row.kind === 'advertisement' && <div className="batch-collect-purposes">{(['brand', 'conversion'] as Purpose[]).map(p => <label key={p}><input type="checkbox" checked={row.purposes.includes(p)} onChange={e => edit(row.asset.id, { purposes: e.target.checked ? [...row.purposes, p] : row.purposes.filter(v => v !== p) })} />{purposeLabel[p]}</label>)}</div>}
        {row.suggested && <small className="muted">当前 AI 分类建议，请核对后收录</small>}
        {skip && <small className="analysis-error">将跳过：{skip}</small>}
       </>}
      </div>
     </li>;
    })}</ul>
    <div className="form-actions"><button className="button secondary" disabled={busy} onClick={close}>{receipts ? '完成并刷新列表' : '取消'}</button>{!receipts && <button className="button primary" disabled={busy || stale || !eligible || !reason.trim()} onClick={submit}>确认收录（{eligible} 张）</button>}</div>
   </div>
  </Modal>}
 </>;
}
