export type Purpose = 'brand' | 'conversion';
export type Status = 'accepted' | 'pending' | 'supplemental' | 'excluded';
export type Permission = 'allowed' | 'unknown' | 'denied';
export type Permissions = { display: Permission; analysis: Permission; export: Permission };
export type DesignAnalysis = { palette: string; typography: string; composition: string; hierarchy: string; imagery: string; reusable_ideas: string; applicability: string; limitations: string };
export type EvidenceKind = 'visual_observation' | 'analysis_inference';
export type ReviewedDesign = Record<keyof DesignAnalysis, { text: string; kind: EvidenceKind }>;
export type ReviewDraft = { source_job_id: string | null; summary_zh: string; design: ReviewedDesign };
export type TaxonomyGroup = 'industry' | 'medium' | 'task';
export type TaxonomyDraft = { tags: Record<TaxonomyGroup, string[]>; note: string };
export type ReviewedTaxonomy = TaxonomyDraft & { schema_version: number; taxonomy_version: string; asset_id: string; revision: number; sha256: string; reviewed_by: string; reviewed_at: string };
export type Taxonomy = { version: string; groups: Record<TaxonomyGroup, Record<string, { label: string; aliases: string[] }>> };
export type ReviewedAnalysis = ReviewDraft & { schema_version: number; asset_id: string; revision: number; sha256: string; reviewed_by: string; reviewed_at: string; model_version?: string; provenance: 'manual_human_review' | 'model_assisted_human_review' };
export type Suggestion = { kind: string; purposes: Purpose[]; brand: string; reason: string; description: string; ocr_text: string; source_image_id: string; provenance: string; model_version?: string; design?: DesignAnalysis | ReviewedDesign; summary_zh?: string };
export type Session = { authenticated: boolean; user?: { actor: string; role: string }; csrf_token?: string; setup_required?: boolean };
export type Asset = {
 id: string; title: string; brand?: string; source_url?: string; image_url?: string; thumbnail_url?: string;
 width?: number; height?: number; status: Status; purposes: Purpose[]; kind: string; reason?: string;
 description?: string; ocr_text?: string; permissions: Permissions; revision: number; created_at?: string; project_id?: string; analysis_suggestion?: Suggestion; analysis_suggestion_revision?: number; analysis_suggestion_job_id?: string | null; reviewed_analysis?: ReviewedAnalysis; reviewed_analysis_status?: 'none' | 'current' | 'stale'; publication_status?: 'published' | 'withdrawn';
 reviewed_taxonomy?: ReviewedTaxonomy; reviewed_taxonomy_status?: 'none' | 'current' | 'stale';
 audit?: Array<{ action?: string; actor?: string; at?: string; created_at?: string; reason?: string }>;
};
export type Source = { id: string; label: string; category: string; verified: boolean; description?: string; requires_credentials?: boolean };
export type Plan = {
 id: string; name: string; source_id: string; enabled: boolean; revision: number;
 readiness: { state: string; label: string; reasons: string[] };
 last_run?: { status: string; started_at?: string; finished_at?: string; reason?: string; imported?: number } | null;
 counts: { fetched: number; accepted: number }; budget: { request_limit: number; used: number; period: string };
 interval_hours: number; next_run?: string | number;
};
export type Summary = { counts: { assets: number; pending: number; accepted: number; supplemental: number }; model?: { configured: boolean }; budget?: unknown };
export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
let csrf = '';
export function setCsrf(value?: string) { csrf = value || ''; }
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
 const res = await fetch(`/workbench/api${path}`, { method, credentials: 'same-origin', headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...(method === 'GET' ? {} : { 'X-CSRF-Token': csrf }) }, body: body === undefined ? undefined : JSON.stringify(body) });
 const data = await res.json().catch(() => null);
 if (!res.ok) {
   if (res.status === 401 && path !== '/session') window.dispatchEvent(new Event('omni:session-expired'));
   const detail = typeof data?.detail === 'string' ? data.detail : '';
   const messages: Record<number, string> = { 401: '登录已失效，请重新登录。', 403: '当前账号没有执行此操作的权限。', 409: '内容已被其他操作更新，请刷新后再试。', 413: '图片过大，请使用较小的文件。', 429: '操作过于频繁或已达到今日预算，请稍后再试。' };
   throw new ApiError(res.status, detail && detail.length < 220 && !/traceback|password|token=|secret|api[_-]?key/i.test(detail) ? detail : messages[res.status] || '服务暂时未完成请求，请稍后重试。');
 }
 return data as T;
}
export function safeUrl(value?: string) { if (!value) return ''; try { const u = new URL(value, window.location.origin); return ['http:', 'https:'].includes(u.protocol) ? u.href : ''; } catch { return ''; } }
export const purposeLabel = { brand: '品牌广告', conversion: '促销与转化' };
export const statusLabel = { accepted: '已收录', pending: '待处理', supplemental: '补充参考', excluded: '已排除' };
export const permissionLabel = { allowed: '已允许', unknown: '待核实', denied: '不允许' };
export function dateLabel(value?: string | number) { if (!value) return '暂无记录'; const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value); return Number.isNaN(date.getTime()) ? '暂无记录' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }); }
