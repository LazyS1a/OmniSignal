// Integration smoke test against the real Python API and production frontend.
// Default: isolated temporary database, synthetic image, no collector/model calls.
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
const results = path.join(frontend, 'test-results');
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'omni-ui-smoke-'));
const username = process.env.SMOKE_USERNAME || 'smoke-admin';
const password = process.env.SMOKE_PASSWORD || crypto.randomBytes(24).toString('base64url');
const viewerPassword = crypto.randomBytes(24).toString('base64url');
let server, browser;
const failures = [];
const checks = [];
function check(condition, message) { if (!condition) throw new Error(message); checks.push(message); }

async function unusedPort() {
 const listener = net.createServer();
 await new Promise((resolve, reject) => listener.once('error', reject).listen(0, '127.0.0.1', resolve));
 const port = listener.address().port;
 await new Promise(resolve => listener.close(resolve));
 return port;
}

async function untilReady(base) {
 for (let attempt = 0; attempt < 100; attempt++) {
  if (server?.exitCode != null) throw new Error('The isolated Python API exited before startup. Check SMOKE_PYTHON and Python dependencies.');
  try { if ((await fetch(`${base}/workbench/api/session`)).ok) return; } catch {}
  await new Promise(resolve => setTimeout(resolve, 200));
 }
 throw new Error('The isolated API did not become ready.');
}

async function login(page, actor, secret, base) {
 await page.goto(`${base}/workbench/`);
 await page.getByRole('textbox', { name: '账号', exact: true }).fill(actor);
 await page.getByLabel('密码', { exact: true }).fill(secret);
 await page.getByRole('button', { name: '登录工作台', exact: true }).click();
 await page.getByRole('heading', { name: '好广告，从这里开始。', exact: true }).waitFor();
}

try {
 await fs.mkdir(results, { recursive: true });
 let base = process.env.SMOKE_BASE_URL;
 const ownedServer = !base;
 if (base) {
  const url = new URL(base);
  if (!['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) || !process.env.SMOKE_USERNAME || !process.env.SMOKE_PASSWORD) throw new Error('Existing-server mode requires a loopback SMOKE_BASE_URL plus SMOKE_USERNAME and SMOKE_PASSWORD. Use only an isolated test database.');
  base = url.origin;
 } else {
  const port = await unusedPort();
  base = `http://127.0.0.1:${port}`;
  const python = process.env.SMOKE_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const code = `import json, os
from pathlib import Path
from PIL import Image, ImageDraw
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.app import create_app
import uvicorn
root=Path(os.environ['SMOKE_ROOT'])
users=[{'username':os.environ['SMOKE_USERNAME'],'role':'admin','password_hash':password_hash(os.environ['SMOKE_PASSWORD'])},{'username':'smoke-viewer','role':'viewer','password_hash':password_hash(os.environ['SMOKE_VIEWER_PASSWORD'])}]
(root/'users.json').write_text(json.dumps(users),encoding='utf-8')
image=Image.new('RGB',(600,800),'#dce8bc')
draw=ImageDraw.Draw(image)
draw.text((40,60),'SYNTHETIC UI TEST - NOT A REAL AD',fill='#34452a')
draw.rectangle((60,170,540,640),outline='#667c49',width=3)
image.save(root/'synthetic-ui-test.png')
uvicorn.run(create_app(root,start_worker=False,frontend=os.environ['SMOKE_FRONTEND']),host='127.0.0.1',port=int(os.environ['SMOKE_PORT']),log_level='error')`;
  server = spawn(python, ['-c', code], { cwd: repository, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, PYTHONPATH: path.join(repository, 'src'), SMOKE_ROOT: temporary, SMOKE_FRONTEND: path.join(frontend, 'dist'), SMOKE_PORT: String(port), SMOKE_USERNAME: username, SMOKE_PASSWORD: password, SMOKE_VIEWER_PASSWORD: viewerPassword, OMNISIGNAL_WORKBENCH_USERS_FILE: path.join(temporary, 'users.json'), OMNISIGNAL_WORKBENCH_MODEL_API_KEY: '', OMNISIGNAL_WORKBENCH_MODEL: '', OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT: '0', OMNISIGNAL_WORKBENCH_SECURE_COOKIE: 'false', OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE: '', OMNISIGNAL_PREPARATION_DIR: '', OMNISIGNAL_WORKBENCH_WORKER: 'false' } });
  server.once('error', () => failures.push('Python process failed to start. Check SMOKE_PYTHON.'));
  // Do not print process stderr: environment-owned paths or provider details may be sensitive.
  server.stderr.on('data', () => {});
 }
 await untilReady(base);
 browser = await chromium.launch({ headless: true, ...(process.env.SMOKE_BROWSER_CHANNEL ? { channel: process.env.SMOKE_BROWSER_CHANNEL } : {}) });
 const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
 const page = await context.newPage();
 const pageErrors = [];
 page.on('pageerror', e => pageErrors.push(e.message));
 await login(page, username, password, base);
 if (ownedServer) {
  await page.getByRole('heading', { name: '第一张好广告，还在路上。', exact: true }).waitFor();
  checks.push('real-empty-state');
 }
 await page.screenshot({ path: path.join(results, '01-library.png'), fullPage: true });
 const image = process.env.SMOKE_IMAGE_FILE || path.join(temporary, 'synthetic-ui-test.png');
 if (!ownedServer && !process.env.SMOKE_IMAGE_FILE) throw new Error('Existing-server mode requires SMOKE_IMAGE_FILE pointing to an explicitly synthetic test image.');
 const title = `合成界面测试图 · 非真实广告 ${crypto.randomBytes(4).toString('hex')}`;
 await page.getByRole('button', { name: '导入素材', exact: true }).first().click();
 let modal = page.getByRole('dialog');
 await modal.getByLabel('选择广告图片').setInputFiles(image);
 await modal.getByRole('textbox', { name: '素材标题', exact: true }).fill(title);
 await modal.getByRole('textbox', { name: '原始来源', exact: true }).fill(`https://example.com/synthetic-ui-test/${title.slice(-8)}`);
 await modal.getByRole('button', { name: '导入待处理', exact: true }).click();
 await page.getByRole('heading', { name: '把值得参考的，留下来。', exact: true }).waitFor();
 // Render a clearly synthetic analysis response without making a paid model
 // call or changing the isolated API's persisted review/permission state.
 const assetRoute = /\/workbench\/api\/assets\/[^/]+$/;
 if (ownedServer) await page.route(assetRoute, async route => {
  const response = await route.fetch();
  const asset = await response.json();
  if (asset.title !== title) return route.fulfill({ response });
  asset.analysis_suggestion_revision = asset.revision;
  asset.analysis_suggestion = { kind: 'reference', purposes: [], brand: '', reason: '仅合成界面验证', description: '', ocr_text: '', source_image_id: asset.id, provenance: 'model_suggestion_requires_review', model_version: 'synthetic-browser-vlm', design: { palette: '合成测试配色', typography: '合成测试字体', composition: '合成测试版式', hierarchy: '合成测试层级', imagery: '合成测试图形', reusable_ideas: '合成测试方法', applicability: '合成测试场景', limitations: '仅测试展示，不构成真实设计分析' } };
  await route.fulfill({ response, json: asset });
 });
 await page.getByRole('heading', { name: title, exact: true }).click();
 modal = page.getByRole('dialog');
 await modal.getByRole('combobox', { name: '内容类型', exact: true }).selectOption('advertisement');
 await modal.getByRole('checkbox', { name: '品牌广告', exact: true }).check();
 await modal.getByRole('textbox', { name: '判断理由', exact: true }).fill('仅验证界面审核流程的合成图，不作为真实广告质量验收。');
 await modal.locator('summary').filter({ hasText: '分析建议' }).click();
 check(await modal.getByRole('button', { name: '分析这张图片', exact: true }).isDisabled(), 'unapproved-image-analysis-disabled');
 if (ownedServer) {
  check(await modal.getByText('本次模型：synthetic-browser-vlm', { exact: true }).isVisible(), 'analysis-actual-model-metadata-visible');
  check(await modal.locator('dt').count() === 8 && await modal.getByText('仅测试展示，不构成真实设计分析', { exact: true }).isVisible(), 'analysis-eight-design-dimensions-visible');
  check(await modal.getByRole('button', { name: '采用建议填写', exact: true }).isDisabled(), 'suggestion-without-real-job-cannot-be-adopted');
  await page.unroute(assetRoute);
 }
 await modal.locator('summary').filter({ hasText: '分析建议' }).click();
 await page.screenshot({ path: path.join(results, '02-review.png'), fullPage: true });
 await modal.getByRole('button', { name: '收录素材', exact: true }).click();
 await modal.waitFor({ state: 'hidden' });
 await page.getByRole('button', { name: '素材库', exact: true }).click();
 await page.getByRole('heading', { name: title, exact: true }).waitFor();
 await page.getByRole('button', { name: '促销与转化', exact: true }).click();
 if (ownedServer) await page.getByRole('heading', { name: '没有符合条件的素材', exact: true }).waitFor();
 await page.getByRole('button', { name: '品牌广告', exact: true }).click();
 await page.getByRole('heading', { name: title, exact: true }).waitFor();
 await page.getByRole('textbox', { name: '搜索标题、品牌或描述', exact: true }).fill('definitely-absent-ui-test-query');
 await page.getByRole('heading', { name: '没有符合条件的素材', exact: true }).waitFor();
 await page.getByRole('button', { name: '清除筛选', exact: true }).click();
 await page.getByRole('heading', { name: title, exact: true }).waitFor();
 checks.push('real-import-review-purpose-filter-search');
 if (ownedServer) {
  await page.getByRole('heading', { name: title, exact: true }).click();
  modal = page.getByRole('dialog');
  await modal.getByRole('combobox', { name: '内容类型', exact: true }).selectOption('reference');
  await modal.getByRole('checkbox', { name: '品牌广告', exact: true }).uncheck();
  await modal.getByRole('textbox', { name: '判断理由', exact: true }).fill('这是一张自建合成设计参考图，不是实际广告，无需广告用途。');
  await modal.locator('summary').filter({ hasText: '发布分类' }).click();
  await modal.getByRole('checkbox', { name: '行业：食品饮料', exact: true }).waitFor();
  check(await modal.locator('.taxonomy-choices input:checked').count() === 0, 'legacy-asset-has-no-fabricated-default-classification');
  await modal.getByRole('checkbox', { name: '行业：食品饮料', exact: true }).check();
  await modal.getByRole('checkbox', { name: '媒介：包装', exact: true }).check();
  await modal.getByRole('checkbox', { name: '设计任务：系列扩展', exact: true }).check();
  const taxonomyConfirmation = modal.getByRole('checkbox', { name: '我已核对行业、媒介和设计任务，确认保存本次分类', exact: true });
  check(!await taxonomyConfirmation.isChecked(), 'taxonomy-is-not-implicitly-confirmed');
  await taxonomyConfirmation.check();
  await modal.getByRole('textbox', { name: '分类补充说明', exact: true }).fill('自有合成包装样张，仅测试分类贯通。');
  check(!await taxonomyConfirmation.isChecked(), 'editing-taxonomy-resets-explicit-confirmation');
  await taxonomyConfirmation.check();
  await page.screenshot({ path: path.join(results, '06-taxonomy-review.png'), fullPage: true });
  await modal.locator('summary').filter({ hasText: '人工审阅的设计分析' }).click();
  await modal.getByRole('textbox', { name: '审核版中文摘要', exact: true }).fill('人工审核的自有合成设计参考：绿色底色与细线矩形构成，供界面回归验证。');
  const labels = ['配色', '字体', '构图与版式', '信息层级', '视觉手法', '可借鉴的方法', '适用场景', '证据与不确定项'];
  for (const label of labels) {
   await modal.getByRole('textbox', { name: `审核版${label}`, exact: true }).fill(`${label}：这是人工核对的合成测试说明，不代表真实案例或投放事实。`);
   await modal.getByRole('combobox', { name: `${label}依据类型`, exact: true }).selectOption(label === '配色' ? 'visual_observation' : 'analysis_inference');
  }
  const confirmation = modal.getByRole('checkbox', { name: '我已逐项核对摘要、设计分析与依据类型，确认本次保存为人工审核版', exact: true });
  check(!await confirmation.isChecked(), 'review-is-not-implicitly-confirmed');
  await confirmation.check();
  await modal.getByRole('textbox', { name: '审核版配色', exact: true }).fill('浅绿色背景和深绿色线框是画面直接可见内容。');
  check(!await confirmation.isChecked(), 'editing-analysis-resets-explicit-confirmation');
  await confirmation.check();
  await page.screenshot({ path: path.join(results, '05-manual-review.png'), fullPage: true });
  await modal.getByRole('button', { name: '收录素材', exact: true }).click();
  await modal.waitFor({ state: 'hidden' });
  const listed = await (await context.request.get(`${base}/workbench/api/assets?status=accepted`)).json();
  const reference = listed.items.find(item => item.title === title);
  check(reference.kind === 'reference' && reference.purposes.length === 0 && reference.status === 'accepted', 'reference-accepted-without-advertising-purpose');
  check(reference.reviewed_analysis_status === 'current' && reference.reviewed_analysis?.summary_zh?.includes('人工审核') && Object.keys(reference.reviewed_analysis.design).length === 8 && reference.reviewed_analysis.design.palette.kind === 'visual_observation' && reference.reviewed_analysis.source_job_id === null, 'manual-reviewed-analysis-persisted-with-eight-evidence-fields');
  check(reference.reviewed_taxonomy_status === 'current' && reference.reviewed_taxonomy.revision === reference.revision && JSON.stringify(reference.reviewed_taxonomy.tags) === JSON.stringify({ industry: ['food-beverage'], medium: ['packaging'], task: ['series'] }), 'reviewed-taxonomy-persists-exact-choice-and-bound-revision');
  await page.getByRole('heading', { name: title, exact: true }).click();
  modal = page.getByRole('dialog');
  await modal.getByRole('textbox', { name: '内容说明', exact: true }).fill('普通编辑，用于验证旧人工审核版本会过期。');
  await modal.getByRole('button', { name: '收录素材', exact: true }).click();
  await modal.waitFor({ state: 'hidden' });
  const stale = await (await context.request.get(`${base}/workbench/api/assets/${reference.id}`)).json();
  check(stale.reviewed_analysis_status === 'stale', 'ordinary-edit-invalidates-reviewed-analysis');
  check(stale.reviewed_taxonomy_status === 'stale', 'ordinary-edit-invalidates-taxonomy');
  await page.getByRole('heading', { name: title, exact: true }).click();
  modal = page.getByRole('dialog');
  await modal.getByText('已过期 · 需要重审', { exact: true }).waitFor();
  await modal.getByText('标签已过期', { exact: true }).waitFor();
  await modal.locator('summary').filter({ hasText: '发布分类' }).click();
  check(await modal.getByRole('checkbox', { name: '媒介：包装', exact: true }).isChecked(), 'saved-taxonomy-survives-reopen');
  await modal.getByRole('checkbox', { name: '我已核对行业、媒介和设计任务，确认保存本次分类', exact: true }).check();
  await modal.locator('summary').filter({ hasText: '人工审阅的设计分析' }).click();
  check(await modal.getByRole('textbox', { name: '审核版配色', exact: true }).inputValue() === '浅绿色背景和深绿色线框是画面直接可见内容。', 'saved-analysis-survives-reopen');
  await modal.getByRole('checkbox', { name: '我已逐项核对摘要、设计分析与依据类型，确认本次保存为人工审核版', exact: true }).check();
  await modal.getByRole('button', { name: '收录素材', exact: true }).click();
  await modal.waitFor({ state: 'hidden' });
  const reviewed = await (await context.request.get(`${base}/workbench/api/assets/${reference.id}`)).json();
  check(reviewed.reviewed_analysis_status === 'current' && reviewed.reviewed_analysis.revision === reviewed.revision, 'explicit-reconfirmation-binds-new-revision');
  check(reviewed.reviewed_taxonomy_status === 'current' && reviewed.reviewed_taxonomy.revision === reviewed.revision, 'explicit-taxonomy-reconfirmation-binds-new-revision');
  const session = await (await context.request.get(`${base}/workbench/api/session`)).json();
  const assistantResponse = await context.request.post(`${base}/workbench/api/assets/${reference.id}/review`, {
   headers: { 'X-CSRF-Token': session.csrf_token }, data: { expected_revision: reviewed.revision,
    reviewed_analysis: { source_job_id: null, review_method: 'assistant', summary_zh: '助手整理的合成测试样图；未经人工逐项复核。', design: reviewed.reviewed_analysis.design },
    reviewed_taxonomy: { tags: reviewed.reviewed_taxonomy.tags, note: '助手分类，用于合成测试。' } }
  });
  check(assistantResponse.ok(), 'assistant-review-accepted-by-real-authenticated-api');
  const assistant = await assistantResponse.json();
  check(assistant.reviewed_analysis.provenance === 'assistant_editorial_review' && assistant.reviewed_analysis.model_version === 'none', 'assistant-provenance-is-not-human-or-model-job');
  await page.getByRole('heading', { name: title, exact: true }).click();
  modal = page.getByRole('dialog');
  await modal.locator('summary').filter({ hasText: '助手整理的设计分析' }).click();
  check(await modal.getByText('已整理 · 当前版本', { exact: true }).isVisible() && await modal.getByText(/AI 助手整理，未经人工逐项复核/).isVisible(), 'assistant-origin-visible-without-human-review-claim');
  check(!await modal.getByRole('checkbox', { name: '我已逐项核对摘要、设计分析与依据类型，确认本次保存为人工审核版', exact: true }).isChecked(), 'assistant-content-is-not-implicitly-human-confirmed');
  await page.screenshot({ path: path.join(results, '08-assistant-provenance.png'), fullPage: true });
  await modal.getByRole('button', { name: '关闭', exact: true }).click();
 }

 await page.getByRole('button', { name: '采集', exact: true }).click();
 await page.getByRole('button', { name: '创建采集计划', exact: true }).click();
 const planTitle = `合成测试计划 · 暂停不联网 ${title.slice(-8)}`;
 await page.getByRole('textbox', { name: '计划名称', exact: true }).fill(planTitle);
 await page.getByRole('button', { name: '创建计划', exact: true }).click();
 const plan = page.locator('article').filter({ has: page.getByRole('heading', { name: planTitle, exact: true }) });
 await plan.waitFor();
 check(await plan.getByRole('button', { name: '运行', exact: true }).isDisabled(), 'new-plan-remains-paused-no-network');
 await page.screenshot({ path: path.join(results, '03-plans.png'), fullPage: true });
 await page.getByRole('button', { name: '设置', exact: true }).click();
 await page.getByRole('heading', { name: '工作台设置', exact: true }).waitFor();
 await page.getByRole('button', { name: '关闭', exact: true }).click();
 await page.setViewportSize({ width: 390, height: 844 });
 await page.getByRole('button', { name: '素材库', exact: true }).click();
 await page.getByRole('heading', { name: title, exact: true }).waitFor();
 await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
 check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), '390px-mobile-no-horizontal-overflow');
 await page.screenshot({ path: path.join(results, '04-mobile.png'), fullPage: true });
 await page.getByRole('button', { name: '账号', exact: true }).click();
 await page.getByRole('button', { name: '退出登录', exact: true }).click();
 await page.getByRole('heading', { name: '进入工作台', exact: true }).waitFor();
 if (ownedServer) {
  await login(page, 'smoke-viewer', viewerPassword, base);
  check(await page.getByRole('button', { name: '导入素材', exact: true }).count() === 0, 'viewer-cannot-import');
  await page.getByRole('heading', { name: title, exact: true }).click();
  await page.getByRole('textbox', { name: '判断理由', exact: true }).waitFor();
  check(await page.getByRole('button', { name: '收录素材', exact: true }).count() === 0, 'viewer-cannot-review');
  await page.getByRole('dialog').locator('summary').filter({ hasText: '发布分类' }).click();
  check(await page.getByRole('checkbox', { name: '媒介：包装', exact: true }).isDisabled() && await page.getByRole('checkbox', { name: '我已核对行业、媒介和设计任务，确认保存本次分类', exact: true }).count() === 0, 'viewer-can-read-but-cannot-edit-taxonomy');
  check(await page.getByRole('dialog').evaluate(dialog => dialog.scrollWidth <= dialog.clientWidth), '390px-taxonomy-dialog-no-horizontal-overflow');
  await page.getByRole('checkbox', { name: '媒介：包装', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(results, '07-mobile-taxonomy.png'), fullPage: true });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
 }
 check(pageErrors.length === 0, 'zero-browser-runtime-errors');
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'passed', checks, real_ad_quality: 'not_evaluated_synthetic_ui_only' }, null, 2));
 console.log(`Passed ${checks.length} browser checks. Screenshots: frontend/test-results/. Synthetic UI fixtures only; no advertising-quality claim.`);
} catch (error) {
 console.error(`Browser smoke failed: ${error.message}`);
 process.exitCode = 1;
} finally {
 if (browser) await browser.close();
 if (server && server.exitCode === null) {
  server.kill();
  await new Promise(resolve => { server.once('exit', resolve); setTimeout(resolve, 3000); });
 }
 const resolved = path.resolve(temporary);
 if (path.dirname(resolved) === path.resolve(os.tmpdir()) && path.basename(resolved).startsWith('omni-ui-smoke-')) await fs.rm(resolved, { recursive: true, force: true });
}
