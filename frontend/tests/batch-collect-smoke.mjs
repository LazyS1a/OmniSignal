// Real local API, production bundle, isolated synthetic images. Never runs a
// collector/model/publication or accepts an external-server URL.
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
const results = path.join(frontend, 'test-results', 'batch-collect');
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'omni-collect-ui-'));
const password = crypto.randomBytes(24).toString('base64url');
const checks = [], reviewRequests = [], forbidden = [], errors = [];
let server, browser, stage = 'startup';
function check(condition, code) { if (!condition) throw new Error(code); checks.push(code); }
async function port() { const s = net.createServer(); await new Promise((a, b) => s.once('error', b).listen(0, '127.0.0.1', a)); const n = s.address().port; await new Promise(a => s.close(a)); return n; }
async function ready(base) { for (let i = 0; i < 100; i++) { if (server.exitCode !== null) throw new Error('isolated-api-exited'); try { if ((await fetch(`${base}/workbench/api/session`)).ok) return; } catch {} await new Promise(r => setTimeout(r, 200)); } throw new Error('isolated-api-not-ready'); }
const code = `import io,json,os
from pathlib import Path
from PIL import Image,ImageDraw
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.app import create_app
from omnisignal.workbench.store import encode
from omnisignal.workbench.review import DESIGN_FIELDS
import uvicorn
root=Path(os.environ['SMOKE_ROOT'])
(root/'users.json').write_text(json.dumps([{'username':role,'role':role,'password_hash':password_hash(os.environ['SMOKE_PASSWORD'])} for role in ('admin','viewer')]),encoding='utf-8')
app=create_app(root,start_worker=False,frontend=os.environ['SMOKE_FRONTEND'])
store=app.state.store
fixtures={}
titles=['Mixed Reference','Mixed Advertisement','Mixed Suggestion','Mixed Unknown','Mixed Stale','Mixed MissingPurpose','Mixed Withdrawn','Conflict One','Conflict Two','Lost One','Lost Two','Forbidden One','Forbidden Two','Expired One','Expired Two','Leave One','Leave Two']+[f'Paging {i:02d}' for i in range(26)]
for n,title in enumerate(titles):
 image=Image.new('RGB',(320,240),(30+n*4,100+n,150)); ImageDraw.Draw(image).text((10,10),'SYNTHETIC TEST '+str(n),fill='white')
 buf=io.BytesIO(); image.save(buf,format='PNG')
 a=store.import_asset({'title':title,'source_url':'https://example.com/synthetic/'+str(n),'description':'Synthetic test fixture only'},buf.getvalue())
 kind='advertisement' if title in ('Mixed Advertisement','Mixed MissingPurpose') else ('uncertain' if title in ('Mixed Suggestion','Mixed Unknown','Mixed Stale') else 'reference')
 body={'expected_revision':a['revision'],'kind':kind,'purposes':['brand'] if title=='Mixed Advertisement' else [],'reason':'Synthetic fixture setup'}
 if title=='Mixed Reference':
  body['reviewed_analysis']={'source_job_id':None,'summary_zh':'自制合成图片的原有审核，用于验证批量收录不会自动提升审核。','design':{key:{'text':'合成图片观察','kind':'visual_observation'} for key in DESIGN_FIELDS}}
  body['permissions']={'display':'allowed','analysis':'unknown','export':'denied'}; body['permission_note']='Self-created test image only'
 a=store.review(a['id'],body,'fixture-setup','admin')
 if title in ('Mixed Suggestion','Mixed Stale','Mixed Withdrawn'):
  with store.db() as db:
   row=db.execute('SELECT document FROM assets WHERE id=?',(a['id'],)).fetchone(); doc=json.loads(row[0])
   if title=='Mixed Withdrawn': doc['publication_status']='withdrawn'
   else:
    doc['analysis_suggestion_revision']=a['revision'] if title=='Mixed Suggestion' else a['revision']-1
    doc['analysis_suggestion']={'kind':'reference','purposes':[],'reason':'Synthetic suggestion, no provider called','brand':'','description':'','ocr_text':'','source_image_id':a['id'],'provenance':'synthetic_fixture'}
   db.execute('UPDATE assets SET document=? WHERE id=?',(encode(doc),a['id']))
 fixtures[title]={'id':a['id'],'revision':a['revision']}
(root/'fixture-ids.json').write_text(json.dumps(fixtures),encoding='utf-8')
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['SMOKE_PORT']),log_level='error')`;

try {
 await fs.mkdir(results, { recursive: true });
 const localPort = await port(), base = `http://127.0.0.1:${localPort}`;
 server = spawn(process.env.SMOKE_PYTHON || 'python', ['-c', code], {
  cwd: repository, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'],
  env: { ...process.env, PYTHONPATH: path.join(repository, 'src'), PYTHONUTF8: '1', SMOKE_ROOT: temporary, SMOKE_FRONTEND: path.join(frontend, 'dist'), SMOKE_PORT: String(localPort), SMOKE_PASSWORD: password, OMNISIGNAL_WORKBENCH_USERS_FILE: path.join(temporary, 'users.json'), OMNISIGNAL_WORKBENCH_WORKER: 'false', OMNISIGNAL_WORKBENCH_MODEL_API_KEY: '', OMNISIGNAL_WORKBENCH_MODEL: '', OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT: '0', OMNISIGNAL_WORKBENCH_SECURE_COOKIE: 'false', OMNISIGNAL_KNOWLEDGE_DIR: path.join(temporary, 'published'), OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE: '', OMNISIGNAL_PREPARATION_DIR: '' }
 });
 server.stderr.on('data', () => {});
 await ready(base);
 const fixtures = JSON.parse(await fs.readFile(path.join(temporary, 'fixture-ids.json'), 'utf8'));
 browser = await chromium.launch({ headless: true, ...(process.env.SMOKE_BROWSER_CHANNEL ? { channel: process.env.SMOKE_BROWSER_CHANNEL } : {}) });
 const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
 const page = await context.newPage();
 page.on('pageerror', e => errors.push(e.message));
 page.on('request', req => {
  const p = new URL(req.url()).pathname;
  if (req.method() === 'POST' && p.endsWith('/review')) reviewRequests.push({ url: req.url(), body: req.postDataJSON() });
  if (req.method() !== 'GET' && /\/analysis|\/publish|\/plans/.test(p)) forbidden.push(p);
 });
 let fault = '', delay = false, faultHit = false, releaseFirstRequest;
 await page.route('**/workbench/api/assets/*/review', async route => {
  if (delay) { delay = false; await new Promise(resolve => { releaseFirstRequest = resolve; }); }
  if (fault && !faultHit) {
   faultHit = true;
   if (fault === 'lost') { await route.fetch(); await route.abort('failed'); return; }
   if (fault === 'forbidden') { await route.fulfill({ status: 403, json: { detail: 'Synthetic revoked permission' } }); return; }
   if (fault === 'expired') { await route.fulfill({ status: 401, json: { detail: 'Synthetic session expired' } }); return; }
  }
  await route.continue();
 });
 async function login(role = 'admin') {
  await page.goto(`${base}/workbench/`);
  if (await page.getByRole('heading', { name: '进入工作台', exact: true }).count()) {
   await page.getByRole('textbox', { name: '账号', exact: true }).fill(role);
   await page.getByLabel('密码', { exact: true }).fill(password);
   await page.getByRole('button', { name: '登录工作台', exact: true }).click();
  }
  await page.getByRole('heading', { name: '好广告，从这里开始。', exact: true }).waitFor();
 }
 async function pending() { await page.getByRole('button', { name: /^待处理/ }).click(); await page.getByRole('heading', { name: '把值得参考的，留下来。' }).waitFor(); }
 async function search(term) {
  const response = page.waitForResponse(r => new URL(r.url()).pathname === '/workbench/api/assets' && new URL(r.url()).searchParams.get('q') === term);
  await page.getByRole('textbox', { name: '搜索标题、品牌或描述' }).fill(term); await response;
  await page.getByRole('checkbox', { name: '全选本页' }).waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="正在加载素材"]'));
 }
 async function selectAll() { await page.getByRole('checkbox', { name: '全选本页' }).check(); }
 async function open() { await page.getByRole('button', { name: '批量收录', exact: true }).click(); await page.getByRole('dialog', { name: '确认批量收录' }).waitFor(); return page.getByRole('dialog'); }
 async function confirm() { await page.getByLabel('本批收录理由', { exact: true }).fill('合成案例有清晰的版式参考价值，仅用于收录流程验收。'); await page.getByRole('button', { name: /^确认收录/ }).click(); }
 async function complete() { await page.getByRole('button', { name: '完成并刷新列表', exact: true }).waitFor(); await page.waitForFunction(() => { const b = [...document.querySelectorAll('button')].find(e => e.textContent === '完成并刷新列表'); return b && !b.disabled; }); }
 async function close() { await page.getByRole('button', { name: '完成并刷新列表', exact: true }).click(); await page.getByRole('dialog').waitFor({ state: 'hidden' }); await page.waitForFunction(() => !document.querySelector('[aria-label="正在加载素材"]')); }
 async function asset(title) { return (await context.request.get(`${base}/workbench/api/assets/${fixtures[title].id}`)).json(); }
 const originalImage = async title => crypto.createHash('sha256').update(await (await context.request.get(`${base}/workbench/api/assets/${fixtures[title].id}/image`)).body()).digest('hex');

 stage = 'mixed-preview';
 await login(); await pending(); await search('Mixed');
 check(await page.getByRole('button', { name: '批量收录', exact: true }).isDisabled(), 'empty-selection-disabled');
 await selectAll(); let modal = await open();
 check(await modal.getByText('已选 7 张 · 3 张可收录。', { exact: false }).isVisible(), 'mixed-preview-excludes-unclassified-ad-without-purpose-and-withdrawn');
 check(await modal.getByLabel('内容类型 Mixed Suggestion', { exact: true }).inputValue() === 'reference', 'fresh-suggestion-visible-for-confirmation');
 check(await modal.getByLabel('内容类型 Mixed Stale', { exact: true }).inputValue() === 'uncertain', 'stale-suggestion-not-applied');
 check(await modal.getByText('当前 AI 分类建议，请核对后收录', { exact: true }).count() === 1, 'suggestion-explicitly-labelled');
 check(await modal.getByRole('button', { name: /^确认收录/ }).isDisabled(), 'reason-required');
 await modal.getByLabel('统一内容类型').selectOption('advertisement');
 check(await modal.getByText('广告素材尚未选择用途', { exact: false }).count() > 0, 'advertisement-purpose-required');
 await modal.getByLabel('统一内容类型').selectOption('');
 const desktopModal = await modal.boundingBox(), desktopAction = await modal.getByRole('button', { name: /^确认收录/ }).boundingBox();
 check(!!desktopModal && !!desktopAction && desktopAction.y >= desktopModal.y && desktopAction.y + desktopAction.height <= Math.min(desktopModal.y + desktopModal.height - 1, page.viewportSize().height), 'desktop-sticky-confirm-button-fully-visible');
 await page.screenshot({ path: path.join(results, 'desktop-confirm.png'), fullPage: true });
 await page.setViewportSize({ width: 390, height: 844 });
 check(await modal.evaluate(el => el.scrollWidth <= el.clientWidth) && await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '390px-dialog-and-page-no-horizontal-overflow');
 const modalBox = await modal.boundingBox(), actionBox = await modal.getByRole('button', { name: /^确认收录/ }).boundingBox();
 check(!!modalBox && !!actionBox && actionBox.y >= modalBox.y && actionBox.y + actionBox.height <= Math.min(modalBox.y + modalBox.height - 1, page.viewportSize().height), '390px-sticky-confirm-button-fully-visible');
 await page.screenshot({ path: path.join(results, 'mobile-confirm.png'), fullPage: true });
 await page.setViewportSize({ width: 1440, height: 1000 });
 await modal.getByRole('button', { name: '取消', exact: true }).click();
 check(reviewRequests.length === 0, 'cancel-submits-nothing');

 stage = 'mixed-commit';
 const before = await asset('Mixed Reference'), hashBefore = await originalImage('Mixed Reference');
 modal = await open(); await modal.getByLabel('本批收录理由').fill('Synthetic batch review reason');
 check(await page.getByRole('button', { name: '批量分析', exact: true }).isDisabled(), 'analysis-mutually-excluded');
 delay = true;
 await modal.getByRole('button', { name: /^确认收录/ }).evaluate(button => { button.click(); button.click(); });
 await modal.getByRole('status').filter({ hasText: '正在处理，请保持页面打开' }).waitFor();
 check(await modal.locator('select,input,textarea').count() === 0, 'first-request-immediately-freezes-editable-fields');
 for (let i = 0; i < 50 && !releaseFirstRequest; i++) await new Promise(resolve => setTimeout(resolve, 20));
 if (!releaseFirstRequest) throw new Error('first-request-not-observed');
 releaseFirstRequest();
 await complete();
 check(reviewRequests.length === 3, 'double-click-submits-each-eligible-item-once');
 check(await modal.getByRole('status').textContent().then(t => t.includes('3 张成功') && t.includes('4 张跳过')), 'mixed-receipt-accurate');
 const after = await asset('Mixed Reference'), ad = await asset('Mixed Advertisement');
 check(after.status === 'accepted' && ad.kind === 'advertisement' && ad.purposes.join() === 'brand', 'real-api-mixed-classifications-persist');
 check(JSON.stringify(after.permissions) === JSON.stringify(before.permissions), 'permissions-unchanged');
 check(await originalImage('Mixed Reference') === hashBefore && after.description === before.description, 'original-image-and-description-preserved');
 check(after.reviewed_analysis_status === 'stale' && JSON.stringify(after.reviewed_analysis) === JSON.stringify(before.reviewed_analysis), 'existing-review-preserved-but-not-promoted');
 check(!(await asset('Mixed Suggestion')).reviewed_analysis && (await asset('Mixed Unknown')).status === 'pending', 'suggestion-not-human-review-and-skipped-item-stays-pending');
 check(reviewRequests.every(r => Object.keys(r.body).sort().join() === ['expected_revision', 'status', 'kind', 'purposes', 'reason'].sort().join()), 'requests-contain-only-collection-fields');
 await close();
 check(await page.getByRole('checkbox', { name: '全选本页' }).isChecked() === false && await page.getByRole('button', { name: '批量收录', exact: true }).isDisabled(), 'completed-batch-clears-selection');

 stage = 'conflict';
 await search('Conflict'); await selectAll(); modal = await open();
 const first = await asset('Conflict One');
 const session = await (await context.request.get(`${base}/workbench/api/session`)).json();
 await context.request.post(`${base}/workbench/api/assets/${first.id}/review`, { headers: { 'X-CSRF-Token': session.csrf_token }, data: { expected_revision: first.revision, reason: 'Concurrent synthetic edit preserved' } });
 const requestsBeforeConflict = reviewRequests.length;
 await confirm(); await complete();
 check(reviewRequests.length === requestsBeforeConflict + 2 && await modal.getByRole('status').textContent().then(t => t.includes('1 张成功') && t.includes('1 张跳过')), 'one-conflict-does-not-block-other-item');
 check((await asset('Conflict One')).reason === 'Concurrent synthetic edit preserved' && (await asset('Conflict One')).status === 'pending', 'conflict-never-overwrites-newer-version');
 await close();

 stage = 'lost-response';
 await search('Lost'); await selectAll(); modal = await open();
 fault = 'lost'; faultHit = false; let start = reviewRequests.length;
 await confirm(); await complete();
 check(reviewRequests.length === start + 1 && await modal.getByRole('status').textContent().then(t => t.includes('1 张结果待核实') && t.includes('1 张未提交')), 'lost-response-is-unknown-and-stops-remaining-no-retry');
 check([await asset('Lost One'), await asset('Lost Two')].filter(a => a.status === 'accepted').length === 1, 'lost-response-committed-asset-remains-accepted');
 fault = ''; await close();

 stage = 'forbidden';
 await search('Forbidden'); await selectAll(); modal = await open();
 fault = 'forbidden'; faultHit = false; start = reviewRequests.length;
 await confirm(); await complete();
 check(reviewRequests.length === start + 1 && await modal.getByRole('status').textContent().then(t => t.includes('1 张失败') && t.includes('1 张未提交')), '403-stops-remaining-items');
 check((await asset('Forbidden One')).status === 'pending' && (await asset('Forbidden Two')).status === 'pending', '403-makes-no-persisted-change');
 fault = ''; await close();

 stage = 'paging';
 await search('Paging'); await selectAll();
 check(await page.getByRole('checkbox', { name: /^选择素材 / }).count() === 24, 'select-all-is-page-scoped');
 await page.getByRole('button', { name: '下一页', exact: true }).click();
 await page.waitForFunction(() => !document.querySelector('[aria-label="正在加载素材"]') && document.querySelectorAll('.asset-select input').length === 2);
 check(await page.getByRole('button', { name: '批量收录', exact: true }).isDisabled(), 'page-change-clears-selection');
 await selectAll(); await search('Mixed');
 check(await page.getByRole('button', { name: '批量收录', exact: true }).isDisabled(), 'search-change-clears-selection');

 stage = 'expiry';
 await search('Expired'); await selectAll(); await open();
 fault = 'expired'; faultHit = false; start = reviewRequests.length;
 await confirm(); await page.getByRole('heading', { name: '进入工作台', exact: true }).waitFor();
 await new Promise(r => setTimeout(r, 200));
 check(reviewRequests.length === start + 1, '401-unmount-stops-remaining-items');
 fault = '';

 stage = 'viewer';
 const viewer = await browser.newContext({ viewport: { width: 390, height: 844 } });
 const viewerPage = await viewer.newPage();
 await viewerPage.goto(`${base}/workbench/`);
 await viewerPage.getByRole('textbox', { name: '账号', exact: true }).fill('viewer');
 await viewerPage.getByLabel('密码', { exact: true }).fill(password);
 await viewerPage.getByRole('button', { name: '登录工作台', exact: true }).click();
 await viewerPage.getByRole('heading', { name: '好广告，从这里开始。' }).waitFor();
 await viewerPage.getByRole('button', { name: /^待处理/ }).click();
 await viewerPage.getByRole('heading', { name: '把值得参考的，留下来。' }).waitFor();
 check(await viewerPage.getByRole('button', { name: '批量收录', exact: true }).count() === 0 && await viewerPage.getByRole('checkbox', { name: /^选择素材 / }).count() === 0, 'viewer-has-no-batch-entry');
 const vs = await (await viewer.request.get(`${base}/workbench/api/session`)).json();
 check((await viewer.request.post(`${base}/workbench/api/assets/${fixtures['Leave One'].id}/review`, { headers: { 'X-CSRF-Token': vs.csrf_token }, data: { expected_revision: 2, status: 'accepted', kind: 'reference', reason: 'Should be denied' } })).status() === 403, 'real-api-rejects-viewer-review');
 check(forbidden.length === 0, 'zero-analysis-collection-or-publication-requests');
 check(errors.length === 0, 'zero-browser-runtime-errors');
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'passed', checks, fixture: 'isolated_generated_images_only', production_mutations: 0, provider_calls: 0 }, null, 2));
 console.log(`Passed ${checks.length} batch collection browser checks. Synthetic fixtures only. Screenshots: frontend/test-results/batch-collect/.`);
} catch (error) {
 console.error(`Batch collection smoke failed at ${stage}: ${error.message}`);
 await fs.writeFile(path.join(results, 'failure.json'), JSON.stringify({ stage, error: error.message, checks }, null, 2)).catch(() => {});
 process.exitCode = 1;
} finally {
 if (browser) await browser.close();
 if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => { server.once('exit', resolve); setTimeout(resolve, 3000); }); }
 const resolved = path.resolve(temporary);
 if (path.dirname(resolved) === path.resolve(os.tmpdir()) && path.basename(resolved).startsWith('omni-collect-ui-')) await fs.rm(resolved, { recursive: true, force: true });
}
