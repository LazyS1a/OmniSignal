import {dateLabel,type Source} from './api';

export default function SourceOverview({sources}:{sources:Source[]}) {
 return <section className="source-overview" aria-label="来源运行状态">
  <div className="source-overview-heading"><div><h2>来源运行状态</h2><p className="muted">计划、会话和实际入池分别显示。单图人工验证不代表持续采集可用。</p></div><span className="tag">每 10 秒刷新</span></div>
  <div className="source-operation-grid">{sources.filter(source=>!['test','research'].includes(source.category)).map(source=>{
   const status=source.operations,quota=status?.quota;
   return <article className="source-operation" key={source.id} data-source-id={source.id}>
    <div className="source-operation-title"><h3>{source.label}</h3><span className={`tag ${status?.state==='running'?'brand':''}`}>{status?.label||'运行状态未知'}</span></div>
    <p className="muted source-operation-description">{source.description?.replaceAll('已验证','已提供适配')||'等待来源说明'}</p>
    <dl className="source-operation-facts">
     <div><dt>采集计划</dt><dd>{status?`${status.plan_count} 个 / ${status.enabled_plan_count} 个启用`:'未知'}</dd></div>
     <div><dt>今日新增入池</dt><dd>{status?.today.imported??'未知'}{status?.today.imported!==null&&status?.today.imported!==undefined?' 张':''}</dd></div>
     <div><dt>最近采集</dt><dd>{status?.last_collection_at?dateLabel(status.last_collection_at):'尚无计划运行记录'}</dd></div>
     <div><dt>访问会话</dt><dd>{status?.session.label||'状态未知'}</dd></div>
     <div><dt>来源日目标</dt><dd>{quota?.quota_configured?`${quota.source_target??'未知'} 张`:'未单独配置'}</dd></div>
     <div><dt>当前可采余额</dt><dd>{quota?.allowed_remaining===null||quota?.allowed_remaining===undefined?'未知':`${quota.allowed_remaining} 张`}</dd></div>
    </dl>
    <p className="source-operation-note">北京时间 · 只计采集器去重入池，不含人工样本，不代表 DeepFlare 已接收。</p>
    {status?.session.checked_at&&<p className="source-operation-note">会话最近检查：{dateLabel(status.session.checked_at)}</p>}
    {status?.collection_evidence.successful_runs?<p className="source-operation-note">已有 {status.collection_evidence.successful_runs} 次运行带回图片，覆盖 {status.collection_evidence.successful_days} 天；持续运行仍需观察。</p>:<p className="source-operation-note">尚无正式计划带回图片的记录。</p>}
    {status?.quota_error&&<p className="source-operation-warning" role="status">{status.quota_error}</p>}
    {status?.last_failure&&<div className="source-operation-warning"><strong>最近未完成原因</strong><p>{status.last_failure.label}</p><small>{dateLabel(status.last_failure.at||undefined)}</small></div>}
   </article>;
  })}</div>
 </section>;
}
