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
 await page.getByRole('heading', { name: title, exact: true }).click();
 modal = page.getByRole('dialog');
 await modal.getByRole('combobox', { name: '内容类型', exact: true }).selectOption('advertisement');
 await modal.getByRole('checkbox', { name: '品牌广告', exact: true }).check();
 await modal.getByRole('textbox', { name: '判断理由', exact: true }).fill('仅验证界面审核流程的合成图，不作为真实广告质量验收。');
 await modal.locator('summary').filter({ hasText: '分析建议' }).click();
 check(await modal.getByRole('button', { name: '分析这张图片', exact: true }).isDisabled(), 'unapproved-image-analysis-disabled');
 await modal.locator('summary').filter({ hasText: '分析建议' }).click();
 await page.screenshot({ path: path.join(results, '02-review.png'), fullPage: true });
 await modal.getByRole('button', { name: '收录为广告素材', exact: true }).click();
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
  check(await page.getByRole('button', { name: '收录为广告素材', exact: true }).count() === 0, 'viewer-cannot-review');
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
