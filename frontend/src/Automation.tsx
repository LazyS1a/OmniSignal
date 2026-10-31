import { useEffect, useState } from 'react';

type Snapshot = {
 collection: { date: string; target: number; new_unique_images: number; remaining: number | null };
 editorial: {
  status: 'enabled' | 'disabled' | 'invalid_config'; running: boolean; auto_publish: boolean;
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
 return <section className="daily-pilot" aria-labelledby="automation-title">
  <div className="daily-pilot-heading"><div><h2 id="automation-title">采集 → 千问分类 → 灵感发布</h2><p className="daily-pilot-schedule">{data?.collection.date || '今日'} · 按北京时间统计新入库图片，重复图片不计入目标</p></div><span className="daily-pilot-state">{!data ? '读取中' : !data.worker_enabled ? '执行器未启动' : data.editorial.status === 'enabled' ? '自动分类与 AI 复核已启用' : data.editorial.status === 'invalid_config' ? '自动流程配置待检查' : '自动复核未启用'}</span></div>
  {error && <p className="daily-pilot-warning" role="alert">自动流程状态暂时无法读取，已有统计可能过时。</p>}
  <dl className="daily-pilot-counts" aria-label="采集与分类统计">{[
   ['今日采集目标', data?.collection.target || null], ['新入库图片', data?.collection.new_unique_images],
   ['AI 复核完成', today?.reviewed], ['新发布图片', today?.published],
  ].map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{typeof value === 'number' ? value : '—'}</dd></div>)}</dl>
  {!!data?.collection.remaining && <p className="daily-pilot-note">今天距采集目标还差 {data.collection.remaining} 张。来源供给不足、站点限流或请求预算用完都会留下实际缺口。</p>}
  {!!today?.permission_blocked && <p className="daily-pilot-warning">{today.permission_blocked} 张尚未记录内部展示或传输依据，暂不发布到 DeepFlare。</p>}
  <p className="daily-pilot-note">{data?.editorial.auto_publish ? '符合条件的素材自动生成带出处的知识包；DeepFlare 拉取并校验后显示。' : '当前未启用自动发布。'} AI 复核与人工审核分别记录；下游接收状态以 DeepFlare 为准。</p>
 </section>;
}
