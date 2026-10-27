// Real Python API + production UI, isolated generated images and fake provider.
// No company credentials, crawlers, paid model calls or production data.
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repository = path.resolve(frontend, '..');
const unlimited = process.env.SMOKE_ANALYSIS_UNLIMITED === 'true';
const results = path.join(frontend, 'test-results', unlimited ? 'batch-analysis-unlimited' : 'batch-analysis');
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'omni-batch-ui-'));
const password = crypto.randomBytes(24).toString('base64url');
const viewerPassword = crypto.randomBytes(24).toString('base64url');
let server, browser, stage = 'startup';
const checks = [];
function check(condition, code) { if (!condition) throw new Error(code); checks.push(code); }
async function unusedPort() { const listener = net.createServer(); await new Promise((resolve, reject) => listener.once('error', reject).listen(0, '127.0.0.1', resolve)); const port = listener.address().port; await new Promise(resolve => listener.close(resolve)); return port; }
async function ready(base) { for (let i = 0; i < 100; i++) { if (server.exitCode != null) throw new Error('fixture-exited'); try { if ((await fetch(`${base}/workbench/api/session`)).ok) return; } catch {} await new Promise(resolve => setTimeout(resolve, 200)); } throw new Error('fixture-not-ready'); }
const fakeCalls = async () => JSON.parse(await fs.readFile(path.join(temporary, 'calls.json'), 'utf8')).count;
async function login(page, base, user, secret) { await page.goto(`${base}/workbench/`); await page.getByRole('textbox', { name: '账号', exact: true }).fill(user); await page.getByLabel('密码', { exact: true }).fill(secret); await page.getByRole('button', { name: '登录工作台', exact: true }).click(); await page.getByRole('heading', { name: '好广告，从这里开始。', exact: true }).waitFor(); }
const previewPath = '/workbench/api/analysis/batches/preview';
const batchPath = '/workbench/api/analysis/batches';
const code = `import io,json,os,time
from pathlib import Path
from PIL import Image,ImageDraw
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench import analysis as module
from omnisignal.workbench.app import create_app
import uvicorn
root=Path(os.environ['SMOKE_ROOT'])
users=[{'username':'batch-admin','role':'admin','password_hash':password_hash(os.environ['SMOKE_PASSWORD'])},{'username':'batch-viewer','role':'viewer','password_hash':password_hash(os.environ['SMOKE_VIEWER_PASSWORD'])}]
(root/'users.json').write_text(json.dumps(users),encoding='utf-8')
class FakeProvider:
 cache_identity='synthetic-batch-ui-v1'
 model_version='synthetic-only'
 calls=0
 def analyze(self,asset,image):
  self.calls+=1
  (root/'calls.json').write_text(json.dumps({'count':self.calls}),encoding='utf-8')
  time.sleep(0.7)
  if asset['title'].startswith('B '): raise ValueError('synthetic-failure')
  return {'purposes':['brand'],'kind':'reference','reason':'SYNTHETIC UI TEST ONLY','description':'Synthetic fixture','ocr_text':'SYNTHETIC','brand':'TEST'}
provider=FakeProvider()
RealService=module.AnalysisService
module.AnalysisService=lambda store:RealService(store,provider)
app=create_app(root,start_worker=True,frontend=os.environ['SMOKE_FRONTEND'])
store=app.state.store
assets=[]
for index in range(20):
 picture=Image.new('RGB',(48,48),(20+index,40+index,60+index))
 data=io.BytesIO();picture.save(data,'PNG')
 store.import_asset({'title':'Pagination fixture '+str(index),'source_url':'https://example.invalid/pagination/'+str(index)},data.getvalue())
labels=[('A Test ready','allowed'),('B Test failure','allowed'),('C Test permission unknown','unknown'),('D Test permission denied','denied'),('E Test already analyzed','allowed'),('F Test response recovery','allowed'),('H Test remaining one','allowed'),('I Test quota limit','allowed')]
for index,(title,permission) in enumerate(labels):
 picture=Image.new('RGB',(360,420),(170+index*9,200-index*8,180+index*5))
 ImageDraw.Draw(picture).text((20,30),'SYNTHETIC UI TEST '+str(index),fill='black')
 data=io.BytesIO();picture.save(data,'PNG')
 asset=store.import_asset({'title':title,'source_url':'https://example.invalid/synthetic/'+str(index)},data.getvalue())
 asset=store.review(asset['id'],{'expected_revision':asset['revision'],'permissions':{'display':'allowed','analysis':permission,'export':'unknown'},'permission_note':'Generated isolated UI test fixture'},'batch-admin','admin')
 assets.append(asset)
seed=RealService(store,provider)
seed.enqueue(assets[4]['id'],assets[4]['revision'],'batch-admin')
seed.run_pending()
(root/'assets.json').write_text(json.dumps([{'id':a['id'],'title':a['title']} for a in assets]),encoding='utf-8')
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['SMOKE_PORT']),log_level='error')`;

try {
 await fs.mkdir(results, { recursive: true });
 const port = await unusedPort(), base = `http://127.0.0.1:${port}`;
 server = spawn(process.env.SMOKE_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-c', code], {
  cwd: repository, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, PYTHONPATH: path.join(repository, 'src'), SMOKE_ROOT: temporary, SMOKE_FRONTEND: path.join(frontend, 'dist'), SMOKE_PORT: String(port), SMOKE_PASSWORD: password, SMOKE_VIEWER_PASSWORD: viewerPassword, OMNISIGNAL_WORKBENCH_USERS_FILE: path.join(temporary, 'users.json'), OMNISIGNAL_WORKBENCH_ANALYSIS_BACKEND: 'direct', OMNISIGNAL_WORKBENCH_MODEL_API_KEY: '', OMNISIGNAL_WORKBENCH_MODEL: '', OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT: unlimited ? 'unlimited' : '5', OMNISIGNAL_WORKBENCH_SECURE_COOKIE: 'false', OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE: '', OMNISIGNAL_PREPARATION_DIR: '', OMNISIGNAL_WORKBENCH_WORKER: 'true' }
 });
 server.stderr.on('data', () => {});
 await ready(base);
 const assets = JSON.parse(await fs.readFile(path.join(temporary, 'assets.json'), 'utf8'));
 browser = await chromium.launch({ headless: true, channel: process.env.SMOKE_BROWSER_CHANNEL || 'chrome' });
 const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
 const page = await context.newPage();
 let pageErrors = 0; const submissions = [];
 page.on('pageerror', () => pageErrors++);
 page.on('request', req => { if (new URL(req.url()).pathname === batchPath && req.method() === 'POST') submissions.push(req.postDataJSON()); });
 await login(page, base, 'batch-admin', password);
 await page.getByRole('button', { name: /^待处理/ }).click();
 await page.getByRole('heading', { name: assets[0].title, exact: true }).waitFor();
 stage = 'pagination-selection';
 await page.getByRole('checkbox', { name: `选择分析 ${assets[0].title}`, exact: true }).check();
 await page.getByRole('button', { name: '下一页', exact: true }).click();
 await page.getByText('已选 0 张', { exact: true }).waitFor();
 check(await page.getByRole('button', { name: '批量分析', exact: true }).isDisabled(), 'page-change-clears-selection');
 await page.getByRole('button', { name: '上一页', exact: true }).click();
 await page.getByRole('textbox', { name: '搜索标题、品牌或描述', exact: true }).fill('Test');
 await page.getByRole('heading', { name: assets[0].title, exact: true }).waitFor();
 await page.getByRole('button', { name: '下一页', exact: true }).waitFor({ state: 'hidden' });
 check(await page.getByRole('checkbox', { name: /^选择分析 / }).count() === 8, 'pending-selection-visible');

 stage = 'preview';
 await page.getByRole('checkbox', { name: '全选本页', exact: true }).check();
 let response = page.waitForResponse(r => new URL(r.url()).pathname === previewPath);
 await page.getByRole('button', { name: '批量分析', exact: true }).click();
 let preview = await (await response).json();
 check(preview.totals.eligible === (unlimited ? 5 : 4) && preview.totals.skipped === (unlimited ? 3 : 4), 'server-preview-quota-and-skip-counts');
 if (unlimited) {
  check(preview.quota.limit === null && preview.quota.remaining === null && preview.quota.unlimited === true, 'unlimited-quota-is-explicit-null');
  await page.getByText(/无每日次数上限 · 今日已调用 1 次/).waitFor();
  check(!preview.items.some(item => item.reason_code === 'budget_exhausted') && !await page.getByText(/今日限额 0 次|剩余 null|今日限额 null/).count(), 'unlimited-preview-text-and-no-quota-skips');
 } else check(preview.items.some(item => item.reason_code === 'budget_exhausted'), 'quota-skip-reason-visible');
 check(await fakeCalls() === 1 && submissions.length === 0, 'preview-does-not-call-model-or-submit-batch');
 await page.getByRole('button', { name: `开始批量分析（${unlimited ? 5 : 4} 张）`, exact: true }).waitFor();
 await page.screenshot({ path: path.join(results, '01-preview-desktop.png'), fullPage: true });
 await page.setViewportSize({ width: 390, height: 844 });
 check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile-preview-no-horizontal-overflow');
 await page.screenshot({ path: path.join(results, '02-preview-mobile.png'), fullPage: true });
 await page.getByRole('button', { name: '暂不分析', exact: true }).click();
 await page.setViewportSize({ width: 1440, height: 1000 });
 // Delay an actual preview response while the user changes the selection.
 // The late response must not restore a stale confirmation or endless loader.
 stage = 'preview-selection-changed';
 let releasePreview, previewStarted;
 const previewGate = new Promise(resolve => { releasePreview = resolve; });
 const previewEntered = new Promise(resolve => { previewStarted = resolve; });
 await page.route('**/workbench/api/analysis/batches/preview', async route => {
  const actual = await route.fetch(); previewStarted(); await previewGate; await route.fulfill({ response: actual });
 });
 await page.getByRole('button', { name: '批量分析', exact: true }).click();
 await previewEntered;
 await page.getByRole('textbox', { name: '搜索标题、品牌或描述', exact: true }).fill('A Test');
 await page.getByRole('checkbox', { name: /^选择分析 / }).first().waitFor();
 await page.getByText('已选 0 张', { exact: true }).waitFor();
 check(await page.getByRole('heading', { name: '确认批量分析', exact: true }).count() === 0, 'changing-filter-closes-inflight-preview');
 response = page.waitForResponse(r => new URL(r.url()).pathname === previewPath);
 releasePreview(); await response;
 await page.getByRole('button', { name: '批量分析', exact: true }).waitFor();
 check(await page.getByRole('heading', { name: '确认批量分析', exact: true }).count() === 0 && await page.getByText('正在读取批次信息…', { exact: true }).count() === 0, 'late-preview-cannot-restore-stale-confirmation-or-loader');
 await page.unroute('**/workbench/api/analysis/batches/preview');
 check(await page.getByRole('button', { name: '批量分析', exact: true }).isDisabled(), 'filter-change-clears-selection');
 await page.getByRole('textbox', { name: '搜索标题、品牌或描述', exact: true }).fill('');
 await page.getByRole('heading', { name: assets[7].title, exact: true }).waitFor();
 for (const asset of assets.slice(0, 5)) await page.getByRole('checkbox', { name: `选择分析 ${asset.title}`, exact: true }).check();
 response = page.waitForResponse(r => new URL(r.url()).pathname === previewPath);
 await page.getByRole('button', { name: '批量分析', exact: true }).click();
 preview = await (await response).json();
 check(preview.totals.eligible === 2 && preview.totals.skipped === 3, 'mixed-eligibility-preview');
 check(await fakeCalls() === 1, 'no-paid-equivalent-call-before-confirmation');

 stage = 'submission-and-progress';
 response = page.waitForResponse(r => new URL(r.url()).pathname === batchPath && r.request().method() === 'POST');
 await page.getByRole('button', { name: '开始批量分析（2 张）', exact: true }).click();
 const first = await (await response).json();
 check(first.totals.submitted === 2 && first.totals.skipped === 3, 'only-eligible-items-enqueued');
 await page.getByRole('heading', { name: '批量分析进度', exact: true }).waitFor();
 await page.reload();
 await page.getByRole('button', { name: /^待处理/ }).click();
 await page.getByRole('button', { name: /^查看最近批次/ }).click();
 await page.getByText('分析完成 · 待复核', { exact: true }).waitFor({ timeout: 20000 });
 await page.getByText('分析失败', { exact: true }).waitFor({ timeout: 20000 });
 check(await fakeCalls() === 3 && submissions.length === 1, 'reload-recovers-progress-without-resubmission');
 const stored = await page.evaluate(() => Object.entries(sessionStorage).filter(([key]) => key.startsWith('omni:analysis-batch:')));
 check(stored.length === 1 && /^[a-f0-9]{32}$/.test(stored[0][1]), 'session-storage-contains-only-batch-id');
 await page.screenshot({ path: path.join(results, '03-progress-desktop.png'), fullPage: true });
 await page.locator('.batch-items').getByRole('button', { name: assets[0].title, exact: true }).click();
 await page.getByRole('dialog').waitFor();
 await page.getByRole('dialog').locator('summary').filter({ hasText: '分析建议' }).click();
 await page.getByText('本次模型：synthetic-only', { exact: true }).waitFor();
 if (unlimited) {
  await page.getByRole('dialog').getByText(/无每日次数上限 · 今日已调用 3 次/).waitFor();
  check(await page.getByRole('dialog').getByText(/无每日次数上限/).count() === 1, 'single-analysis-unlimited-usage-label');
 }
 check(await page.getByRole('dialog').getByRole('button', { name: '收录素材', exact: true }).count() === 1, 'analysis-results-remain-pending-review');
 await page.getByRole('button', { name: '关闭', exact: true }).click();

 stage = 'ambiguous-response';
 await page.getByRole('checkbox', { name: `选择分析 ${assets[5].title}`, exact: true }).check();
 await page.getByRole('button', { name: '批量分析', exact: true }).click();
 await page.getByRole('button', { name: '开始批量分析（1 张）', exact: true }).waitFor();
 let lost = false, blockRecovery = true;
 await page.route('**/workbench/api/analysis/batches/**', async route => {
  const pathname = new URL(route.request().url()).pathname;
  if (route.request().method() === 'GET' && /^\/workbench\/api\/analysis\/batches\/[a-f0-9]{32}$/.test(pathname) && blockRecovery) return route.abort('failed');
  return route.continue();
 });
 await page.route('**/workbench/api/analysis/batches', async route => {
  if (route.request().method() === 'POST' && !lost) { lost = true; await route.fetch(); return route.abort('failed'); }
  return route.continue();
 });
 await page.getByRole('button', { name: '开始批量分析（1 张）', exact: true }).click();
 await page.getByRole('button', { name: '用同一请求安全重试', exact: true }).waitFor();
 blockRecovery = false;
 await page.getByRole('button', { name: '用同一请求安全重试', exact: true }).click();
 await page.getByRole('heading', { name: '批量分析进度', exact: true }).waitFor();
 await page.getByText('分析完成 · 待复核', { exact: true }).waitFor({ timeout: 20000 });
 check(submissions.length === 3 && JSON.stringify(submissions[1]) === JSON.stringify(submissions[2]), 'lost-response-retries-exact-request-id-and-payload');
 check(await fakeCalls() === 4, 'idempotent-retry-does-not-analyze-twice');
 await page.unroute('**/workbench/api/analysis/batches');
 await page.unroute('**/workbench/api/analysis/batches/**');

 stage = 'read-only-and-mobile';
 await page.setViewportSize({ width: 390, height: 844 });
 check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile-progress-no-horizontal-overflow');
 await page.screenshot({ path: path.join(results, '04-progress-mobile.png'), fullPage: true });
 await page.getByRole('button', { name: '账号', exact: true }).click();
 await page.getByRole('button', { name: '退出登录', exact: true }).click();
 await login(page, base, 'batch-viewer', viewerPassword);
 await page.getByRole('button', { name: /^待处理/ }).click();
 await page.getByRole('heading', { name: assets[0].title, exact: true }).waitFor();
 check(await page.getByRole('button', { name: '批量分析', exact: true }).count() === 0 && await page.getByRole('checkbox', { name: /^选择分析 / }).count() === 0, 'viewer-has-no-batch-actions');
 check(await page.getByRole('button', { name: /^查看最近批次/ }).count() === 0, 'other-user-does-not-see-prior-batch');
 check(pageErrors === 0, 'zero-browser-runtime-errors');
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'passed', checks, provider: 'synthetic-only', paid_calls: 0, fixture_calls: await fakeCalls() }, null, 2));
 console.log(`Passed ${checks.length} batch browser checks. Synthetic provider only; 0 paid calls.`);
} catch {
 // Never print Playwright exception strings/call logs: these may include cookies.
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'failed', stage, checks, error: 'BATCH_BROWSER_CHECK_FAILED' }, null, 2));
 console.error(`BATCH_BROWSER_CHECK_FAILED at ${stage}`);
 process.exitCode = 1;
} finally {
 if (browser) await browser.close();
 if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => { server.once('exit', resolve); setTimeout(resolve, 3000); }); }
 const resolved = path.resolve(temporary);
 if (path.dirname(resolved) === path.resolve(os.tmpdir()) && path.basename(resolved).startsWith('omni-batch-ui-')) await fs.rm(resolved, { recursive: true, force: true });
}
