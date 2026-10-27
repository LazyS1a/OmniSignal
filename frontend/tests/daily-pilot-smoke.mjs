// Production UI + real authenticated Python route, isolated status fixtures only.
// No crawlers, provider credentials, model calls, real assets or production data.
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
const results = path.join(frontend, 'test-results', 'daily-pilot');
const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'omni-daily-pilot-ui-'));
const secret = crypto.randomBytes(24).toString('base64url');
const endpoint = '/workbench/api/analysis/daily-pilot';
const checks = [];
let server, browser, stage = 'startup';
function check(value, name) { if (!value) throw new Error(name); checks.push(name); }
async function unusedPort() { const listener = net.createServer(); await new Promise((resolve, reject) => listener.once('error', reject).listen(0, '127.0.0.1', resolve)); const port = listener.address().port; await new Promise(resolve => listener.close(resolve)); return port; }
async function ready(base) { for (let i = 0; i < 100; i++) { if (server.exitCode != null) throw new Error('fixture-exited'); try { if ((await fetch(`${base}/workbench/api/session`)).ok) return; } catch {} await new Promise(resolve => setTimeout(resolve, 200)); } throw new Error('fixture-not-ready'); }
const policy = { start_date: '2026-10-27', end_date: '2026-11-02', timezone: 'Asia/Shanghai', weekdays: [0, 1, 2, 3, 4], hour: 16, minute: 0, target: 30, review_target_min: 10, review_target_max: 20 };
const snapshot = (changes = {}) => ({ enabled: true, state: 'waiting_window', diagnostic: null, policy, current_day: '2026-10-27', report: null, recent: [], fixture_worker_enabled: true, ...changes });
const report = (changes = {}) => ({ day: '2026-10-27', state: 'finished', batch_id: 'a'.repeat(32), planned_images: 30, eligible_at_plan: 7, submitted: 7, analysed_images: 5, failed: 1, queued: 0, running: 1, projects_covered: 3, submitted_projects: 4, skip_reasons: { permission_required: 32, already_analyzed: 2 }, pending_at_plan: 41, examined: 41, scan_limited: false, submission_shortfall: 23, analysis_shortfall: 25, requires_human_review: 5, review_target_min: 10, review_target_max: 20, reviewed_count: null, updated_at: '2026-10-27T08:15:00Z', ...changes });
async function fixture(value) { await fs.writeFile(path.join(temporary, 'snapshot.json'), JSON.stringify(value)); }
const python = `import json,os
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException
from omnisignal.workbench.auth import password_hash
from omnisignal.workbench.app import create_app
import uvicorn
root=Path(os.environ['SMOKE_ROOT'])
(root/'users.json').write_text(json.dumps([{'username':'pilot-viewer','role':'viewer','password_hash':password_hash(os.environ['SMOKE_PASSWORD'])}]),encoding='utf-8')
app=create_app(root,start_worker=False,frontend=os.environ['SMOKE_FRONTEND'])
def snapshot():
 value=json.loads((root/'snapshot.json').read_text(encoding='utf-8'))
 if value.pop('fixture_error',False):raise HTTPException(503,'isolated_fixture_unavailable')
 app.state.collector=SimpleNamespace() if value.pop('fixture_worker_enabled',True) else None
 return value
app.state.daily_pilot.snapshot=snapshot
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['SMOKE_PORT']),log_level='error')`;

try {
 await fs.mkdir(results, { recursive: true });
 await fixture(snapshot());
 const port = await unusedPort(), base = `http://127.0.0.1:${port}`;
 server = spawn(process.env.SMOKE_PYTHON || (process.platform === 'win32' ? 'python' : 'python3'), ['-c', python], {
  cwd: repository, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe'], env: { ...process.env, PYTHONPATH: path.join(repository, 'src'), SMOKE_ROOT: temporary, SMOKE_FRONTEND: path.join(frontend, 'dist'), SMOKE_PORT: String(port), SMOKE_PASSWORD: secret, OMNISIGNAL_WORKBENCH_USERS_FILE: path.join(temporary, 'users.json'), OMNISIGNAL_WORKBENCH_ANALYSIS_BACKEND: 'direct', OMNISIGNAL_WORKBENCH_MODEL_API_KEY: '', OMNISIGNAL_WORKBENCH_MODEL: '', OMNISIGNAL_WORKBENCH_NOVA_TOKEN: '', OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG: '', OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT: '0', OMNISIGNAL_WORKBENCH_SECURE_COOKIE: 'false', OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE: '', OMNISIGNAL_PREPARATION_DIR: '', OMNISIGNAL_WORKBENCH_WORKER: 'false' }
 });
 server.stderr.on('data', () => {});
 await ready(base);
 check((await fetch(base + endpoint)).status === 401, 'anonymous-pilot-route-denied');
 browser = await chromium.launch({ headless: true, channel: process.env.SMOKE_BROWSER_CHANNEL || 'chrome' });
 const context = await browser.newContext({ viewport: { width: 1440, height: 1100 } });
 const page = await context.newPage();
 await page.clock.install();
 await page.addInitScript(() => { window.__pilotVisibility = 'visible'; Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => window.__pilotVisibility }); });
 const requests = []; let pageErrors = 0;
 page.on('pageerror', () => pageErrors++);
 page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/workbench/api/')) requests.push({ method: request.method(), path: new URL(request.url()).pathname }); });
 await page.goto(`${base}/workbench/`);
 await page.getByRole('textbox', { name: '账号', exact: true }).fill('pilot-viewer');
 await page.getByLabel('密码', { exact: true }).fill(secret);
 await page.getByRole('button', { name: '登录工作台', exact: true }).click();
 await page.getByRole('heading', { name: '好广告，从这里开始。', exact: true }).waitFor();
 requests.length = 0;
 check(await page.getByRole('heading', { name: '每日试运行', exact: true }).count() === 0, 'pilot-card-absent-from-asset-home');
 await page.getByRole('button', { name: /^待处理/ }).click();
 check(await page.getByRole('heading', { name: '每日试运行', exact: true }).count() === 0, 'pilot-card-absent-from-pending-images');
 await page.getByRole('button', { name: '采集', exact: true }).click();
 const card = page.getByRole('region', { name: '每日试运行', exact: true });
 const refresh = page.getByRole('button', { name: '刷新每日试运行状态', exact: true });
 await card.getByText('等待今日运行时间', { exact: true }).waitFor();
 check(await card.getByText('周一至周五 · 北京时间 16:00 · 试点至 11月2日', { exact: true }).count() === 1, 'schedule-explicit-shanghai-weekdays-end-date');
 check(await card.getByText('每日自动目标 30 张', { exact: true }).count() === 1 && await card.locator('dd').allTextContents().then(values => values.every(value => value === '—')), 'not-run-renders-target-with-unknown-counts-not-success-zero');
 check((await card.textContent()).includes('人工审核目标 10–20 张 / 日') && (await card.textContent()).includes('这是目标，人工完成量尚未统计'), 'manual-review-target-never-presented-as-completed');
 check(await card.getByRole('button').count() === 1, 'read-only-card-has-only-refresh-action');
 await page.screenshot({ path: path.join(results, '01-waiting-desktop.png'), fullPage: true });

 stage = 'partial-progress';
 await fixture(snapshot({ state: 'running', report: report({ state: 'running' }) }));
 await refresh.click();
 await card.getByText('今日分析进行中', { exact: true }).waitFor();
 check(JSON.stringify(await card.locator('dd').allTextContents()) === JSON.stringify(['7', '5', '0', '1']), 'report-counts-display-actual-submitted-completed-queued-failed');
 check((await card.textContent()).includes('分析权限待确认 32 张') && (await card.textContent()).includes('已有分析建议 2 张') && (await card.textContent()).includes('今日入队比目标少 23 张'), 'shortfall-shows-server-reasons-and-counts');
 check((await card.textContent()).includes('当前待人工复核 5 张') && (await card.textContent()).includes('正在分析 1 张'), 'pending-human-review-and-running-count-are-explicit');
 await page.screenshot({ path: path.join(results, '02-running-desktop.png'), fullPage: true });
 await page.setViewportSize({ width: 390, height: 844 });
 check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile-card-no-horizontal-overflow');
 await page.screenshot({ path: path.join(results, '03-running-mobile.png'), fullPage: true });
 await page.setViewportSize({ width: 1440, height: 1100 });

 stage = 'visible-read-only-refresh';
 const beforePoll = requests.filter(r => r.path === endpoint).length;
 await fixture(snapshot({ state: 'finished', report: report({ analysed_images: 6, running: 0, analysis_shortfall: 24, requires_human_review: 6 }) }));
 await page.clock.runFor(31000);
 await card.getByText('今日批次已结束 · 有失败', { exact: true }).waitFor();
 check(requests.filter(r => r.path === endpoint).length > beforePoll, 'visible-card-refreshes-after-thirty-seconds');
 await page.evaluate(() => { window.__pilotVisibility = 'hidden'; document.dispatchEvent(new Event('visibilitychange')); });
 const beforeHidden = requests.filter(r => r.path === endpoint).length;
 await page.clock.runFor(61000);
 check(requests.filter(r => r.path === endpoint).length === beforeHidden, 'hidden-page-does-not-poll-pilot');
 const visibleRefresh = page.waitForResponse(response => new URL(response.url()).pathname === endpoint);
 await page.evaluate(() => { window.__pilotVisibility = 'visible'; document.dispatchEvent(new Event('visibilitychange')); });
 await visibleRefresh;
 await refresh.waitFor({ state: 'visible' });

 stage = 'failure-and-recovery';
 await fixture(snapshot({ fixture_error: true }));
 await refresh.click();
 await card.getByRole('alert').waitFor();
 check((await card.getByRole('alert').textContent()).includes('上次读取的快照，可能已经过时'), 'fetch-failure-clearly-marks-stale-snapshot');
 check((await card.locator('dd').allTextContents())[1] === '6', 'fetch-failure-does-not-fabricate-zero-success');
 await fixture(snapshot({ state: 'no_eligible', report: report({ state: 'no_eligible', eligible_at_plan: 0, submitted: 0, analysed_images: 0, failed: 0, running: 0, projects_covered: 0, submitted_projects: 0, submission_shortfall: 30, analysis_shortfall: 30, requires_human_review: 0 }) }));
 await refresh.click();
 await card.getByText('暂无可分析素材', { exact: true }).waitFor();
 check(await card.getByRole('alert').count() === 0 && (await card.textContent()).includes('未新增分析任务'), 'no-eligible-is-honest-and-clears-fetch-error');
 await fixture(snapshot({ state: 'blocked', diagnostic: 'batch_lookup_failed' }));
 await refresh.click();
 await card.getByText('自动入队已暂停', { exact: true }).waitFor();
 check((await card.textContent()).includes('批次状态暂时无法核对。已暂停自动入队，请管理员核查'), 'diagnostic-pauses-instead-of-claiming-success');

 stage = 'lifecycle-states';
 for (const [state, label] of [['disabled', '自动计划已关闭'], ['not_started', '今日尚未运行'], ['weekend', '周末不自动运行'], ['expired_intent', '今日计划已过期'], ['submission_uncertain', '提交结果待核对'], ['ended', '试点已结束']]) {
  await fixture(snapshot({ state, enabled: state !== 'disabled' }));
  await refresh.click();
  await card.getByText(label, { exact: true }).waitFor();
 }
 check((await card.textContent()).includes('后续不会继续自动入队'), 'disabled-not-started-weekend-expired-uncertain-ended-states');
 await page.screenshot({ path: path.join(results, '04-ended-desktop.png'), fullPage: true });
 await fixture(snapshot({ state: 'waiting_window', fixture_worker_enabled: false }));
 await refresh.click();
 await card.getByText('执行器未启动', { exact: true }).waitFor();
 check((await card.textContent()).includes('当前不会自动入队'), 'stopped-worker-is-not-shown-as-scheduled-success');
 stage = 'first-read-failure';
 await fixture(snapshot({ fixture_error: true }));
 await page.reload();
 await page.getByRole('button', { name: '采集', exact: true }).click();
 await card.getByRole('alert').waitFor();
 check(await card.locator('dd').count() === 0 && !(await card.textContent()).includes('每日自动目标'), 'first-read-failure-does-not-invent-plan-or-counts');
 check((await card.textContent()).includes('计划状态暂时无法读取'), 'first-read-failure-is-explicit');
 await fixture(snapshot({ state: 'waiting_window' }));
 await page.reload();
 await page.getByRole('button', { name: '采集', exact: true }).click();
 await card.getByText('等待今日运行时间', { exact: true }).waitFor();
 check(requests.every(r => r.method === 'GET'), 'reload-refresh-and-status-reading-never-post');
 check(pageErrors === 0, 'zero-browser-runtime-errors');
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'passed', checks, fixture: 'isolated-status-only', model_calls: 0, business_writes: 0, state_reads: requests.filter(r => r.path === endpoint).length }, null, 2));
 console.log(`Passed ${checks.length} daily-pilot browser checks. Isolated status fixtures; 0 model calls and 0 business writes.`);
} catch {
 await fs.writeFile(path.join(results, 'summary.json'), JSON.stringify({ status: 'failed', stage, checks, error: 'DAILY_PILOT_BROWSER_CHECK_FAILED' }, null, 2));
 console.error(`DAILY_PILOT_BROWSER_CHECK_FAILED at ${stage}`);process.exitCode = 1;
} finally {
 if (browser) await browser.close();
 if (server && server.exitCode === null) { server.kill(); await new Promise(resolve => { server.once('exit', resolve); setTimeout(resolve, 3000); }); }
 const resolved = path.resolve(temporary);
 if (path.dirname(resolved) === path.resolve(os.tmpdir()) && path.basename(resolved).startsWith('omni-daily-pilot-ui-')) await fs.rm(resolved, { recursive: true, force: true });
}
