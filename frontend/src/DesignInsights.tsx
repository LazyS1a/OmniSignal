import { type Asset, type AnalysisDetails } from './api';

const profiles: Record<string, string> = { 'packaging-analysis': '包装设计', 'brand-visual-analysis': '品牌视觉', 'static-ad-analysis': '静态广告', 'general-analysis': '通用设计' };
export const processingLabels = { waiting_analysis: '等待分析', technical_failure: '技术失败', quality_issue: '内容需处理', unclassified: '分类待确认', stale: '资料已更新', ready: '待自动收录', published: '已发布' };

export default function DesignInsights({ asset, details }: { asset: Asset; details?: AnalysisDetails | null }) {
 const quality = asset.analysis_quality;
 const stale = quality?.status === 'stale' || asset.analysis_details_status === 'stale' || asset.processing_group === 'stale';
 const aiReviewed = asset.reviewed_analysis?.provenance === 'model_automated_review' && asset.reviewed_analysis_status === 'current';
 const issues = quality?.issues || details?.quality.issues || [];
 const card = quality?.inspiration_card || details?.inspiration_card;
 const profile = quality?.profile || details?.profile;
 const hasPermission = asset.publication_use_status === 'internal_policy' || (asset.permissions.display === 'allowed' && asset.permissions.export === 'allowed');
 const target = asset.publication_target;
 const published = target ? target.status === 'published' : asset.publication_status === 'published', withdrawn = asset.publication_status === 'withdrawn' || target?.status === 'withdrawn';
 if (!details && (!quality || quality.status === 'legacy' && !asset.analysis_suggestion && !asset.reviewed_analysis)) return null;
 return <section className="design-insights" aria-labelledby="design-insights-title">
  <div className="insights-heading"><h3 id="design-insights-title">设计分析与灵感卡</h3>{profile && <span className="tag brand">{profiles[profile] || '设计分析'}</span>}</div>
  <ol className="insights-progress" aria-label="素材处理进度">
   <li><strong>AI 分析</strong><span>{stale ? '结果已过期' : details ? '已生成' : quality?.status === 'legacy' ? '历史分析' : '暂无新版详情'}</span></li>
   <li><strong>AI 内容初审</strong><span>{stale ? '需要更新依据' : quality?.status === 'passed' ? aiReviewed ? '通过 · 已收录' : '通过 · 待收录' : quality?.status === 'legacy' ? '沿用历史审核记录' : quality?.status === 'missing' ? '缺少质检记录' : '存在待处理项'}</span></li>
   <li><strong>{target?.kind === 'library' ? '公司灵感库' : '知识包'}</strong><span>{withdrawn ? '已永久撤回' : published ? '已生成 · 下游接收另查' : target?.status === 'stale' ? '交付后资料已变更' : target?.status === 'unavailable' ? '发布状态待核验' : target?.kind === 'library' ? '等待共享库出包' : !hasPermission ? '待补用途依据' : '尚未发布'}</span></li>
  </ol>
  {stale && <p className="analysis-error" role="status">素材或来源资料已更新。以下是历史分析，不能作为当前收录或发布依据。</p>}
  {withdrawn && <p className="analysis-error">该素材已永久撤回，分析结果不会使它重新进入发布流程。</p>}
  <p className="muted">AI 内容初审与人工审核分别记录。正常素材可按自动策略收录、发布；有疑点的素材留在待处理区。</p>
  {target && <p className="muted">当前目标：{target.label}{target.version ? ` · 交付版本 ${target.version}` : ' · 尚无该目标的交付版本'}。下游是否接收以 DeepFlare 为准。</p>}
  {asset.publication_use_status === 'internal_policy' && (!target || target.status === 'published') && <p className="muted">本图按内部用途策略交付，用途记录与 AI 内容分析分别保存。</p>}
  {details?.route_reason && <p className="muted">分析方向依据：{details.route_reason}</p>}
  {card && <div className={`inspiration-card${stale ? ' stale' : ''}`} aria-label={stale ? '历史灵感卡' : '灵感卡'}>
   <dl>{([['设计任务', card.task], ['可借鉴的方法', card.method], ['适用场景', card.applicability], ['使用边界', card.limitations]] as const).map(([label, text]) => <div key={label}><dt>{label}</dt><dd>{text || '尚无可靠结论'}</dd></div>)}</dl>
   <p className="muted">{aiReviewed && !stale ? 'AI 初审已通过，方法仍是分析推断。' : '分析建议，尚不能视为已完成审核。'}原图与出处见左侧。</p>
  </div>}
  {issues.length > 0 && <div className="insights-issues" aria-label="具体待处理项"><h4>需要留意的地方</h4>{issues.map((issue, i) => <article key={`${issue.code}-${i}`} className={issue.severity === 'block' ? 'blocking' : 'advisory'}>
   <strong>{issue.severity === 'block' ? '暂停自动处理' : '保留不确定项'} · {issue.message}</strong>
   {issue.evidence && <p>依据：{issue.evidence}</p>}{issue.action && <p>处理建议：{issue.action}</p>}
  </article>)}</div>}
  {details && <details className="insights-evidence"><summary>查看观察、推断与未知项</summary>
   <h4>图中直接观察</h4>{details.observations.length ? <ul>{details.observations.map(o => <li key={o.id}><span className="evidence-id">{o.id}</span> {o.text}</li>)}</ul> : <p className="muted">未记录直接观察</p>}
   {details.source_statements.length > 0 && <><h4>来源描述</h4><ul>{details.source_statements.map((s, i) => <li key={i}>{s.text}<blockquote>{s.source_quote}</blockquote></li>)}</ul></>}
   <h4>分析推断</h4>{details.inferences.length ? <ul>{details.inferences.map((inference, i) => <li key={i}>{inference.text}<small>所依据的观察：{inference.evidence_ids.join('、') || '未提供'}</small></li>)}</ul> : <p className="muted">未记录额外推断</p>}
   <h4>尚不能确认</h4>{details.unknowns.length ? <ul>{details.unknowns.map((value, i) => <li key={i}>{value}</li>)}</ul> : <p className="muted">本次未列出其他未知项，不代表所有事实均已核实。</p>}
   <p className="muted">模型 {details.model_version} · 分析规则 {details.skill_version} · 校验规则 {details.validator_version} · 来源版本 {details.source.revision}</p>
  </details>}
 </section>;
}
