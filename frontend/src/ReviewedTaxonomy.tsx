import { useEffect, useState } from 'react';
import { dateLabel, request, type Asset, type Taxonomy, type TaxonomyDraft, type TaxonomyGroup } from './api';

const groups: Array<[TaxonomyGroup, string]> = [['industry', '行业'], ['medium', '媒介'], ['task', '设计任务']];
export function taxonomyDraft(asset?: Asset): TaxonomyDraft {
 const saved = asset?.reviewed_taxonomy;
 return saved ? { tags: { industry: [...saved.tags.industry], medium: [...saved.tags.medium], task: [...saved.tags.task] }, note: saved.note } : { tags: { industry: [], medium: [], task: [] }, note: '' };
}
export function taxonomyError(value: TaxonomyDraft) {
 if (!value.tags.industry.length || !value.tags.medium.length) return '分类审核需要至少一个行业和一个媒介；未知时请保留待处理，不要猜选。';
 if (value.tags.industry.includes('other') && !value.note.trim()) return '选择其他行业时，请填写具体行业和分类依据。';
 return '';
}

export default function ReviewedTaxonomyForm({ asset, value, confirmed, onChange, onConfirm, canEdit }: { asset: Asset; value: TaxonomyDraft; confirmed: boolean; onChange: (value: TaxonomyDraft) => void; onConfirm: (value: boolean) => void; canEdit: boolean }) {
 const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null), [error, setError] = useState('');
 const [attempt, setAttempt] = useState(0);
 useEffect(() => { let alive = true; setError(''); request<Taxonomy>('/taxonomy').then(data => { if (alive) setTaxonomy(data); }).catch(e => { if (alive) setError(e.message); }); return () => { alive = false; }; }, [attempt]);
 const status = asset.reviewed_taxonomy_status || 'none';
 function toggle(group: TaxonomyGroup, id: string) { const old = value.tags[group]; onChange({ ...value, tags: { ...value.tags, [group]: old.includes(id) ? old.filter(item => item !== id) : [...old, id] } }); }
 return <details className="detail-section reviewed-taxonomy-section"><summary>发布分类 <span className={`review-status ${status}`}>{status === 'current' ? '分类已审核' : status === 'stale' ? '标签已过期' : '分类待确认'}</span></summary>
  <p className="muted">行业和媒介用于灵感库筛选。数字界面仅指网页与界面设计，不表示所有数字图片。没有依据时保留待处理。</p>
  {asset.reviewed_taxonomy && <p className="review-provenance">上次分类审核：{asset.reviewed_taxonomy.reviewed_by} · {dateLabel(asset.reviewed_taxonomy.reviewed_at)} · 版本 {asset.reviewed_taxonomy.revision}</p>}
  {status === 'stale' && <p className="analysis-error">素材已修改，旧标签不能作为新发布的分类依据；请核对后重新确认。</p>}
  {error ? <p role="alert">分类字典加载失败。<button type="button" className="text-button" onClick={() => setAttempt(n => n + 1)}>重试分类字典</button></p> : !taxonomy ? <p className="muted" aria-busy="true">正在读取分类字典…</p> : <>
   <div className="taxonomy-groups">{groups.map(([group, label]) => <fieldset key={group} disabled={!canEdit}><legend>{label}{group === 'task' ? '（可不选）' : '（至少一项）'}</legend><div className="taxonomy-choices">{Object.entries(taxonomy.groups[group]).map(([id, item]) => <label key={id}><input type="checkbox" aria-label={`${label}：${item.label}`} checked={value.tags[group].includes(id)} onChange={() => toggle(group, id)} />{item.label}</label>)}</div></fieldset>)}</div>
   <label>分类补充说明<textarea aria-label="分类补充说明" value={value.note} readOnly={!canEdit} maxLength={2000} rows={2} placeholder="记录分类依据；选择其他行业时必填。" onChange={e => onChange({ ...value, note: e.target.value })} /></label>
   {canEdit && <label className="review-confirm"><input type="checkbox" checked={confirmed} onChange={e => onConfirm(e.target.checked)} />我已核对行业、媒介和设计任务，确认保存本次分类</label>}
  </>}
  {canEdit && <p className="muted">勾选后随收录或保存操作提交。普通编辑会使旧审核过期；如已有设计分析，请同时核对并确认该分析，避免发布时仍需重审。</p>}
 </details>;
}
