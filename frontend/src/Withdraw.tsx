import { useState } from 'react';
import { request, type Asset } from './api';

export default function Withdraw({ asset, isAdmin, onDone }: { asset: Asset; isAdmin: boolean; onDone: () => void }) {
 const [confirm, setConfirm] = useState(false), [reason, setReason] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
 if (!asset.publication_status) return null;
 if (asset.publication_status === 'withdrawn') return <div className="notice">撤回通知已发布，下游接收尚未确认。该素材已排除，原图与历史仍保留；同一素材标识不能再次发布。</div>;
 async function withdraw() { setBusy(true); setError(''); try { await request('/withdraw', 'POST', { asset_ids: [asset.id], reason: reason.trim() }); onDone(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
 return <div className="publication-notice"><p>这张素材已经发布。变更收录状态或收紧展示、导出权限前，需要先撤回发布。</p>{isAdmin && (confirm ? <><label>撤回原因<textarea aria-label="撤回原因" value={reason} onChange={e => setReason(e.target.value)} maxLength={1000} rows={2} placeholder="说明为什么不再向下游提供这张素材" /></label><p className="muted">确认后会生成签名撤回通知，并将素材移至已排除。原图与记录保留，同一标识不能再次发布；下游是否收到仍需独立确认。</p>{error && <p className="analysis-error" role="alert">{error}</p>}<div className="form-actions"><button className="button small secondary" disabled={busy} onClick={() => setConfirm(false)}>取消</button><button className="button small primary" disabled={busy || !reason.trim()} onClick={withdraw}>{busy ? '正在发布撤回通知…' : '确认撤回发布'}</button></div></> : <button className="button small secondary" onClick={() => setConfirm(true)}>撤回发布</button>)}</div>;
}
