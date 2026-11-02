import { useEffect, useState } from 'react';

type Snapshot = {
 collection: { date: string; target: number; new_unique_images: number; remaining: number | null };
 editorial: {
  status: 'enabled' | 'disabled' | 'invalid_config'; running: boolean; auto_publish: boolean; publication_authorized?: boolean;
  publication_recipient?: { kind: 'library'; library_id: string } | null; target_delivered_revisions?: number | null;
  target_latest_release?: { release_id: string; sequence: number } | null;
  today?: { reviewed: number; published: number; awaiting_review: number; permission_blocked: number };
  last_tick?: { at: string; blocked: number; blocked_reasons: Record<string, number> } | null;
 };
 worker_enabled: boolean;
};

export default function Automation({ revision }: { revision: number }) {
 const [data, setData] = useState<Snapshot | null>(null), [error, setError] = useState(false);
 useEffect(() => {
  let alive = true, busy = false;
  const controller = new AbortController();
  async function load() {
   if (busy || document.visibilityState === 'hidden') return;
   busy = true;
   try {
    const response = await fetch('/workbench/api/automation', { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(12000)]) });
    if (response.status === 401) window.dispatchEvent(new Event('omni:session-expired'));
    if (!response.ok) throw new Error('unavailable');
    const value = await response.json() as Snapshot;
    if (!value.collection || !value.editorial || !Number.isInteger(value.collection.new_unique_images)) throw new Error('invalid');
    if (alive) { setData(value); setError(false); }
   } catch { if (alive) setError(true); }
   finally { busy = false; }
  }
  void load();
  const timer = setInterval(() => void load(), 30000);
  return () => { alive = false; controller.abort(); clearInterval(timer); };
 }, [revision]);
 const today = data?.editorial.today;
 const localReadPending = data?.editorial.last_tick?.blocked_reasons?.local_review_io_retry_wait;
 const localReadFailed = data?.editorial.last_tick?.blocked_reasons?.local_review_io_retry_exhausted;
 return <section className="daily-pilot" aria-labelledby="automation-title">
  <div className="daily-pilot-heading"><div><h2 id="automation-title">采集 → 千问分类 → 灵感发布</h2><p className="daily-pilot-schedule">{data?.collection.date || '今日'} · 按北京时间统计新入库图片，重复图片不计入目标</p></div><span className="daily-pilot-state">{!data ? '读取中' : !data.worker_enabled ? '执行器未启动' : data.editorial.status === 'enabled' ? '自动分类与 AI 复核已启用' : data.editorial.status === 'invalid_config' ? '自动流程配置待检查' : '自动复核未启用'}</span></div>
  {error && <p className="daily-pilot-warning" role="alert">自动流程状态暂时无法读取，已有统计可能过时。</p>}
  <dl className="daily-pilot-counts" aria-label="采集与分类统计">{[
   ['今日采集目标', data?.collection.target || null], ['新入库图片', data?.collection.new_unique_images],
   ['AI 复核完成', today?.reviewed], ['新发布图片', today?.published],
  ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{typeof value === 'number' ? value : '—'}</dd></div>)}</dl>
  {!!data?.collection.remaining && <p className="daily-pilot-note">今天距采集目标还差 {data.collection.remaining} 张。来源供给不足、站点限流或请求预算用完都会留下实际缺口。</p>}
  {data?.editorial.publication_authorized && <p className="daily-pilot-note">已启用公司内部灵感库用途策略：AI 检查合格后自动出包，在 DeepFlare 点击“接收最新灵感”即可导入。</p>}
  {data?.editorial.publication_recipient?.kind === 'library' && <p className="daily-pilot-note">当前发布目标：DeepFlare 公司灵感库。已为该库出包 {data.editorial.target_delivered_revisions ?? 0} 个素材版本，各品牌引用同一库。{data.editorial.target_latest_release ? `最新交付版本：${data.editorial.target_latest_release.release_id}。` : '正在等待首个共享发布包。'}</p>}
  {!!today?.permission_blocked && <p className="daily-pilot-warning">{today.permission_blocked} 张尚未记录内部展示或传输依据，暂不发布到 DeepFlare。</p>}
  {!!localReadPending && <p className="daily-pilot-note">{localReadPending} 张素材暂时无法读取原图，正在等待本地复核；不会重新调用模型。</p>}
  {!!localReadFailed && <p className="daily-pilot-warning">{localReadFailed} 张素材连续读取原图失败，已暂停本地复核，请管理员检查文件。已有分析结果保留。</p>}
  <p className="daily-pilot-note">{data?.editorial.auto_publish ? '内容初审通过且用途依据齐全的素材自动生成带出处的知识包；存在疑点的素材隔离待处理。在 DeepFlare 点击接收，后台校验并导入后显示。' : '当前未启用自动发布：分类与内容初审继续运行，发布包等待手动发布或管理员启用自动策略。'} AI 复核与人工审核分别记录；下游接收状态以 DeepFlare 为准。</p>
 </section>;
}
