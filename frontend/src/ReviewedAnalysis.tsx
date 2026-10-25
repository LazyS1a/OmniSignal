import { dateLabel, type Asset, type ReviewDraft, type ReviewedDesign, type EvidenceKind, type Suggestion } from './api';

export const designLabels: Record<keyof ReviewedDesign, string> = {
 palette: '配色', typography: '字体', composition: '构图与版式', hierarchy: '信息层级',
 imagery: '视觉手法', reusable_ideas: '可借鉴的方法', applicability: '适用场景', limitations: '证据与不确定项',
};
export const designKeys = Object.keys(designLabels) as Array<keyof ReviewedDesign>;
export function emptyDraft(): ReviewDraft {
 return { source_job_id: null, summary_zh: '', design: Object.fromEntries(designKeys.map(key => [key, { text: '', kind: 'analysis_inference' }])) as ReviewedDesign };
}
export function savedDraft(asset: Asset): ReviewDraft {
 const saved = asset.reviewed_analysis;
 // Reconfirming a saved review is independent human review. An old model job
 // is never silently reused as fresh evidence for a newer asset revision.
 return saved ? { source_job_id: null, summary_zh: saved.summary_zh, design: saved.design } : emptyDraft();
}
export function suggestionDraft(suggestion: Suggestion, jobId: string): ReviewDraft {
 return { source_job_id: jobId, summary_zh: (suggestion.summary_zh || suggestion.description || '').slice(0, 6000), design: Object.fromEntries(designKeys.map(key => {
  const value = suggestion.design?.[key];
  return [key, typeof value === 'object' && value ? { text: value.text.slice(0, 1500), kind: value.kind === 'visual_observation' ? 'visual_observation' : 'analysis_inference' } : { text: (value || '').slice(0, 1500), kind: 'analysis_inference' }];
 })) as ReviewedDesign };
}

export default function ReviewedAnalysisForm({ asset, value, confirmed, onChange, onConfirm, canEdit }: { asset: Asset; value: ReviewDraft; confirmed: boolean; onChange: (value: ReviewDraft) => void; onConfirm: (value: boolean) => void; canEdit: boolean }) {
 const status = asset.reviewed_analysis_status || 'none';
 return <details className="detail-section reviewed-analysis-section"><summary>人工审阅的设计分析 <span className={`review-status ${status}`}>{status === 'current' ? '已审核 · 当前版本' : status === 'stale' ? '已过期 · 需要重审' : '尚未审核'}</span></summary>
  <p className="muted">下面的内容由你核对后保存为审核版。可以独立编写，也可以先采用有效模型建议；仅采用建议不会完成审核。</p>
  {asset.reviewed_analysis && <p className="review-provenance">上次审核：{asset.reviewed_analysis.reviewed_by} · {dateLabel(asset.reviewed_analysis.reviewed_at)} · 版本 {asset.reviewed_analysis.revision}{asset.reviewed_analysis.model_version && asset.reviewed_analysis.model_version !== 'none' ? ` · 辅助模型 ${asset.reviewed_analysis.model_version}` : ' · 独立人工审阅'}</p>}
  {status === 'stale' && <p className="analysis-error">素材在上次审核后发生了修改。这份旧分析不能作为当前发布依据，请逐项核对后重新确认。</p>}
  {value.source_job_id ? <div className="analysis-status">草稿来自当前素材的有效模型分析记录；保存时服务端会再次核验。<button type="button" className="text-button" onClick={() => onChange({ ...value, source_job_id: null })} disabled={!canEdit}>改为独立人工审阅</button></div> : <p className="muted">本次按独立人工审阅保存，不借用旧模型任务作为新证据。</p>}
  <label>中文摘要<textarea aria-label="审核版中文摘要" value={value.summary_zh} readOnly={!canEdit} maxLength={6000} rows={4} placeholder="用中文说明这张素材是什么、设计重点是什么，以及可以怎样借鉴。" onChange={e => onChange({ ...value, summary_zh: e.target.value })} /></label>
  <div className="review-design-fields">{designKeys.map(key => <div className="review-design-field" key={key}>
   <div className="review-design-heading"><label htmlFor={`review-design-${key}`}>{designLabels[key]}</label><select aria-label={`${designLabels[key]}依据类型`} value={value.design[key].kind} disabled={!canEdit} onChange={e => onChange({ ...value, design: { ...value.design, [key]: { ...value.design[key], kind: e.target.value as EvidenceKind } } })}><option value="visual_observation">视觉观察</option><option value="analysis_inference">分析推断</option></select></div>
   <textarea id={`review-design-${key}`} aria-label={`审核版${designLabels[key]}`} value={value.design[key].text} readOnly={!canEdit} maxLength={1500} rows={2} placeholder="无法判断可留空；区分图上可见的事实和你的推断。" onChange={e => onChange({ ...value, design: { ...value.design, [key]: { ...value.design[key], text: e.target.value } } })} />
  </div>)}</div>
  {canEdit && <label className="review-confirm"><input type="checkbox" checked={confirmed} onChange={e => onConfirm(e.target.checked)} />我已逐项核对摘要、设计分析与依据类型，确认本次保存为人工审核版</label>}
  {canEdit && <p className="muted">勾选后，点击下方收录或保存待处理才会提交审核版。未勾选时只保存其他素材信息；已有审核版会因版本变化而过期。修改本表后需要重新勾选。</p>}
 </details>;
}
