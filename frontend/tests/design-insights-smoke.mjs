// UI contract fixtures against the production bundle on loopback only.
// This test does NOT demonstrate model quality, publication or receiver delivery.
import { chromium } from 'playwright';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const checks = [], errors = [], forbidden = [], queries = [];
const groups = ['waiting_analysis', 'technical_failure', 'quality_issue', 'unclassified', 'stale', 'ready'];
const design = Object.fromEntries(['palette', 'typography', 'composition', 'hierarchy', 'imagery', 'reusable_ideas', 'applicability', 'limitations'].map(k => [k, { text: '自制测试夹具，仅验证界面交互。', kind: 'analysis_inference' }]));
const details = {
 schema_version: 1, skill_version: 'design-skills-1', prompt_version: 'prompt-1', validator_version: 'validator-1', job_id: 'job-one', model_version: 'synthetic-ui-model',
 source: { asset_id: 'ready-one', revision: 1, sha256: 'a'.repeat(64), input_binding: 'b'.repeat(64) },
 profile: 'packaging-analysis', route_reason: '图中存在正面包装与口味区分，仅是合成测试。',
 observations: [{ id: 'O1', field: 'palette', text: '大面积绿色区块配白色标题。' }],
 source_statements: [{ text: '来源自述为样稿', source_quote: '本作品为样稿' }],
 inferences: [{ field: 'reusable_ideas', text: '可保留标题位置，用颜色区分系列。', evidence_ids: ['O1'] }], unknowns: ['包装背面结构不可确认。'],
 inspiration_card: { task: '合成包装系列', method: '保留识别锚点，以色彩区分口味。', applicability: '有系列变化的包装。', limitations: '缺少实际材料与使用效果依据。', evidence_ids: ['O1'] },
 quality: { status: 'passed', issues: [{ code: 'unknown_material', field: 'material', severity: 'warning', evidence: '图中无法核对材质', action: '保留未知项', message: '材料未知不阻断设计方法' }] },
};
const baseAsset = { title: '合成包装案例', status: 'pending', kind: 'reference', purposes: [], revision: 1, reason: '合成测试', permissions: { analysis: 'allowed', display: 'unknown', export: 'unknown' }, source_url: 'https://example.com/synthetic', reviewed_analysis_status: 'none', reviewed_taxonomy_status: 'none' };
const fixtures = Object.fromEntries(groups.map(group => [group, { ...structuredClone(baseAsset), id: `${group}-one`, title: `合成 ${group}`, processing_group: group, analysis_quality: { status: group === 'ready' ? 'passed' : group === 'stale' ? 'stale' : group === 'quality_issue' ? 'needs_attention' : 'legacy', issues: [], ...(group === 'ready' || group === 'stale' || group === 'quality_issue' ? { profile: details.profile, inspiration_card: details.inspiration_card, source_job_id: details.job_id } : {}) }, ...(group === 'ready' || group === 'stale' || group === 'quality_issue' ? { analysis_details: details } : {}) }]));
fixtures.quality_issue.analysis_quality.issues = [{ code: 'missing_evidence', field: 'brand', severity: 'block', evidence: '图上没有可辨认品牌', action: '删除无法确认的品牌事实', message: '品牌缺少图像依据' }];
fixtures.stale.revision = 4;
const accepted = { ...fixtures.ready, id: 'accepted-one', title: '已收录合成案例', status: 'accepted', revision: 2, processing_group: 'published', publication_status: 'published', permissions: { analysis: 'allowed', display: 'allowed', export: 'allowed' }, reviewed_analysis_status: 'current', reviewed_analysis: { schema_version: 2, asset_id: 'ready-one', revision: 2, sha256: 'a'.repeat(64), reviewed_by: 'system:auto-editorial', reviewed_at: '2026-11-02T00:00:00Z', summary_zh: '合成案例，用于检验 AI 初审状态。', design, provenance: 'model_automated_review', model_version: 'synthetic-ui-model' } };
function check(condition, code) { if (!condition) throw new Error(code); checks.push(code); }
const server = http.createServer(async (req, res) => {
 try {
  const url = new URL(req.url, 'http://127.0.0.1');
  const relative = url.pathname.startsWith('/workbench/assets/') ? `assets/${path.basename(url.pathname)}` : 'index.html';
  const content = await fs.readFile(path.join(frontend, 'dist', relative));
  res.writeHead(200, { 'content-type': relative.endsWith('.js') ? 'text/javascript' : relative.endsWith('.css') ? 'text/css' : 'text/html' }); res.end(content);
 } catch { res.writeHead(404); res.end(); }
});
let browser;
try {
 await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
 const base = `http://127.0.0.1:${server.address().port}`;
 browser = await chromium.launch({ headless: true, channel: process.env.SMOKE_BROWSER_CHANNEL || 'chrome' });
 const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
 page.on('pageerror', error => errors.push(error.message));
 await page.route('**/workbench/api/**', async route => {
  const req = route.request(), url = new URL(req.url()), p = url.pathname.replace('/workbench/api', '');
  if (req.method() !== 'GET') { forbidden.push(p); return route.fulfill({ status: 403, json: { detail: 'UI contract test is read only' } }); }
  let value;
  if (p === '/session') value = { authenticated: true, user: { actor: 'ui-fixture', role: 'admin' }, csrf_token: 'synthetic' };
  else if (p === '/summary') value = { counts: { assets: 8, accepted: 1, pending: 7, supplemental: 0 }, model: { configured: true } };
  else if (p === '/assets') {
   queries.push(Object.fromEntries(url.searchParams));
   const group = url.searchParams.get('processing_group');
   const items = url.searchParams.get('status') === 'accepted' ? [accepted] : group ? [fixtures[group]] : Object.values(fixtures);
   value = { items, total: group ? 1 : 48, page_size: 24 };
  } else if (p.endsWith('/analysis')) value = { items: [] };
  else if (p.startsWith('/assets/')) value = [accepted, ...Object.values(fixtures)].find(a => p === `/assets/${a.id}`);
  else if (p === '/analysis/config') value = { configured: true, status: 'ready', request_limit: 120, used: 0 };
  else if (p === '/taxonomy') value = { version: 'nova-1', groups: { industry: {}, medium: {}, task: {} } };
  else if (p === '/plans') value = { items: [], sources: [] };
  else if (p === '/automation') value = { worker_enabled: true, collection: { date: '2026-11-02', target: 100, new_unique_images: 100, remaining: 0 }, editorial: { status: 'enabled', running: true, auto_publish: true, publication_authorized: true, today: { reviewed: 10, published: 3, awaiting_review: 0, permission_blocked: 7 } } };
  else value = {};
  await route.fulfill({ json: value || {} });
 });
 await page.goto(`${base}/workbench/`);
 await page.getByRole('heading', { name: '已收录合成案例', exact: true }).click();
 let modal = page.getByRole('dialog');
 await modal.getByRole('heading', { name: '设计分析与灵感卡' }).waitFor();
 check(await modal.getByText('通过 · 已收录', { exact: true }).isVisible(), 'AI-review-revision-increment-remains-current');
 check(await modal.getByText('已生成 · 下游接收另查', { exact: true }).isVisible(), 'published-does-not-claim-receiver-receipt');
 check(await modal.getByText('保留识别锚点，以色彩区分口味。', { exact: true }).isVisible(), 'inspiration-method-visible');
 await fs.mkdir(path.join(frontend, 'test-results', 'design-insights'), { recursive: true });
 await page.screenshot({ path: path.join(frontend, 'test-results', 'design-insights', 'desktop-current.png'), fullPage: true });
 await modal.locator('summary').filter({ hasText: '查看观察、推断与未知项' }).click();
 check(await modal.getByText('图中直接观察', { exact: true }).isVisible() && await modal.getByRole('heading', { name: '分析推断', exact: true }).isVisible(), 'observations-and-inferences-separated');
 check(await modal.getByText('包装背面结构不可确认。', { exact: true }).isVisible(), 'unknowns-remain-explicit');
 check(await modal.getByText('所依据的观察：O1', { exact: true }).isVisible(), 'inference-links-to-evidence-id');
 await modal.locator('summary').filter({ hasText: '自动 AI 复核的设计分析' }).click();
 check(await modal.getByText('AI 已初审 · 当前版本', { exact: true }).isVisible() && !await modal.getByRole('checkbox', { name: /我已逐项核对摘要/ }).isChecked(), 'AI-review-never-prechecks-human-approval');
 await modal.getByRole('button', { name: '关闭', exact: true }).click();
 await page.getByRole('button', { name: /^待处理/ }).click();
 await page.getByRole('heading', { name: '合成 waiting_analysis', exact: true }).waitFor();
 await page.getByRole('button', { name: '下一页', exact: true }).click();
 await page.waitForFunction(() => document.querySelector('.pagination')?.textContent.includes('2 / 2'));
 await page.getByRole('checkbox', { name: '选择素材 合成 waiting_analysis', exact: true }).check();
 for (const group of groups) {
  await page.getByRole('combobox', { name: '待处理原因', exact: true }).selectOption(group);
  await page.getByRole('heading', { name: `合成 ${group}`, exact: true }).waitFor();
  check(queries.at(-1).processing_group === group && queries.at(-1).page === '1', `server-group-filter-${group}-resets-pagination`);
  check(await page.locator('.asset-card').count() === 1, `group-${group}-shows-only-server-results`);
 }
 check(!await page.getByRole('checkbox', { name: '选择素材 合成 ready', exact: true }).isChecked(), 'changing-group-clears-selection');
 await page.getByRole('heading', { name: '合成 ready', exact: true }).click(); modal = page.getByRole('dialog'); await modal.getByRole('heading', { name: '设计分析与灵感卡' }).waitFor();
 await modal.getByRole('heading', { name: '设计分析与灵感卡' }).waitFor();
 check(await modal.getByText('待补用途依据', { exact: true }).isVisible(), 'passed-QA-does-not-grant-display-export-permission');
 await modal.getByRole('button', { name: '关闭', exact: true }).click();
 await page.getByRole('combobox', { name: '待处理原因' }).selectOption('quality_issue');
 await page.getByRole('heading', { name: '合成 quality_issue', exact: true }).click(); modal = page.getByRole('dialog'); await modal.getByRole('heading', { name: '设计分析与灵感卡' }).waitFor();
 check(await modal.getByText('暂停自动处理 · 品牌缺少图像依据', { exact: true }).isVisible(), 'blocking-issue-visible-with-specific-cause');
 check(await modal.getByText('处理建议：删除无法确认的品牌事实', { exact: true }).isVisible(), 'specific-remediation-visible');
 await modal.getByRole('button', { name: '关闭', exact: true }).click();
 await page.getByRole('combobox', { name: '待处理原因' }).selectOption('stale');
 await page.getByRole('heading', { name: '合成 stale', exact: true }).click(); modal = page.getByRole('dialog'); await modal.getByRole('heading', { name: '设计分析与灵感卡' }).waitFor();
 check(await modal.getByLabel('历史灵感卡').isVisible() && await modal.getByText(/不能作为当前收录或发布依据/).isVisible(), 'stale-result-labelled-historical');
 await page.setViewportSize({ width: 390, height: 844 });
 check(await modal.evaluate(el => el.scrollWidth <= el.clientWidth + 1), 'mobile-modal-has-no-horizontal-overflow');
 const output = path.join(frontend, 'test-results', 'design-insights'); await fs.mkdir(output, { recursive: true });
 await modal.getByRole('heading', { name: '设计分析与灵感卡' }).scrollIntoViewIfNeeded();
 await page.screenshot({ path: path.join(output, 'mobile-stale.png'), fullPage: true });
 await modal.getByRole('button', { name: '关闭', exact: true }).click();
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.getByRole('button', { name: '采集', exact: true }).click();
 await page.getByText(/内容初审通过且用途依据齐全的素材自动生成/).waitFor();
 check(await page.getByText(/下游接收状态以 DeepFlare 为准/).isVisible(), 'automation-copy-retains-receipt-boundary');
 check(await page.getByText(/已启用公司内部灵感库用途策略/).isVisible(), 'operator-policy-coverage-visible');
 check(await page.getByText(/点击“接收最新灵感”即可导入/).isVisible(), 'manual-deepflare-receive-path-visible');
 check(forbidden.length === 0, 'no-collection-analysis-review-or-publication-writes');
 check(errors.length === 0, 'no-runtime-browser-errors');
 await fs.writeFile(path.join(output, 'checks.json'), JSON.stringify({ kind: 'synthetic-ui-contract-checks', checks }, null, 2));
 console.log(`Passed ${checks.length} design insights UI checks. Synthetic UI contracts only; no model or publication calls.`);
} catch (error) { console.error(`Design insights UI check failed: ${error.message}`); process.exitCode = 1; }
finally { if (browser) await browser.close(); await new Promise(resolve => server.close(resolve)); }
