# 2026-10-26｜Omni A 端人工分类贯通工作日志

## 本轮结果与边界

已在本地工作区完成“人工分类 → 当前素材修订 → 签名发布包”的实现、回归与浏览器验证。行业、媒介和设计任务沿用 `nova-1`，没有扩展风格字典，也没有调用模型推断标签。实现和验证期间没有提交；随后仅将本日志列明的分类改动做成本地提交，未 push、打包发布、操作服务器或变更真实素材权限。

开始与核查时的 Git 基线为 `060cd03f4263872aedcad1f363f940c8efce8bc0`，分支为 `variant/ad-workbench`。工作区原有 README、POTW 采集代码及试点材料仍保留；本日志不把这些既有改动记成本轮实现。

## 实现与自审

1. 新增 `reviewed_taxonomy` JSON 元数据，不增加 SQLite 列，也不批量重写旧素材。请求只接受 `tags` 和 `note`，审核人、时间、版本、图片 hash 由服务端绑定。
2. 只接受 `knowledge/taxonomy.py` 中的标准 ID；行业和媒介至少一项，任务可以为空。“其他行业”必须写说明；未知 ID、中文别名、重复标签、额外分组、伪造审核身份和过长说明会被拒绝，且事务不推进修订号。
3. 保存使用原有 `expected_revision` 并发控制。分类与分析都绑定保存后的同一修订号；普通编辑使旧审核过期。旧审核内容保留以便重新核对，但不被静默续期。
4. operator 可以审核分类，viewer 只读；修改用途权限仍需要 admin。字典读取需要已有登录会话，未新增匿名写入口。
5. 发布使用当前审核标签，移除之前自动写入的 `other/digital`。发布仍校验展示、导出权限、图片实际 hash 和现有审核状态，分类缺失或过期会阻止整批发布。
6. 前端没有默认选项，也不会自动确认。改变标签或分类说明后取消确认；viewer 的选项禁用。已人工查看桌面和 390px 手机截图，表单无横向溢出。

自审未发现阻断本轮交接的问题。需要保留的行为限制是：只改分类也会增加素材修订号，因此已存在的设计分析需要同时复核；这遵循现有审核绑定规则，界面与操作文档均已明确说明。

## 对接契约

认证字典接口：`GET /workbench/api/taxonomy`，返回 `{version, groups}`。

分类提交示例（合成材料示例，不代表真实素材授权）：

```json
{
  "expected_revision": 2,
  "reviewed_taxonomy": {
    "tags": {
      "industry": ["food-beverage"],
      "medium": ["packaging"],
      "task": ["series"]
    },
    "note": "人工核对的自有合成包装样张"
  }
}
```

新发布包保持 schema 1 / nova-1：

- `case.tags`：与审核后的 `industry`、`medium`、`task` 数组一致。
- `case.editorial.material_kind`：原有 `advertisement` / `reference`。
- `case.editorial.taxonomy_reviewed`：`true`。
- `case.editorial.reviewed_taxonomy`：包含 `schema_version: 1`、`taxonomy_version: "nova-1"`、`asset_id`、`revision`、`sha256`、`reviewed_by`、`reviewed_at`、`tags`、`note`。

素材 API 派生 `reviewed_taxonomy_status: none | current | stale`，状态不是客户端可写字段。

## 旧素材与旧包兼容

- 未分类旧素材仍能查看、编辑和保留原有状态；不伪造分类，重新发布前必须完成人工分类。
- 既有签名包不重写、不重新签名，原接收协议继续可读。
- 旧包中 `editorial.legacy_medium_fallback: true` 表示历史兼容值，不能作为真实媒介分类，也不应参与分类筛选命中。消费端应提示待补分类。
- `digital` 的字典含义是数字界面，不能泛指图片文件；`other` 也不能用于掩盖未审核。

## 文件清单

以下 17 个文件属于本轮实现、测试或文档范围：

| 文件 | 变更 |
| --- | --- |
| `src/omnisignal/workbench/taxonomy.py` | 新增严格校验、审核绑定与状态计算 |
| `src/omnisignal/workbench/store.py` | 分类保存、图片校验、审核审计；保留既有 POTW 来源配置 |
| `src/omnisignal/workbench/review.py` | API 返回派生分类状态 |
| `src/omnisignal/workbench/publishing.py` | 分类发布门禁、标签与审核证据透传 |
| `src/omnisignal/workbench/app.py` | 认证字典接口 |
| `frontend/src/ReviewedTaxonomy.tsx` | 新增人工分类表单 |
| `frontend/src/App.tsx` | 表单状态、提交与发布提示 |
| `frontend/src/api.ts` | 分类请求与返回类型 |
| `frontend/src/style.css` | 分类表单布局 |
| `frontend/tests/smoke.mjs` | 增加真实 API 浏览器分类流程回归 |
| `tests/test_workbench_taxonomy.py` | 新增 30 项分类回归 |
| `tests/test_workbench_publishing.py` | 发布夹具改为明确审核分类，校验包字段 |
| `tests/test_workbench_reviewed_analysis.py` | 现有分析夹具增加分类，保留过期审核门禁 |
| `tests/test_workbench_security.py` | 安全夹具增加分类，继续覆盖不可逆撤回门禁 |
| `src/omnisignal/knowledge/PROTOCOL.md` | 新分类契约和旧包语义 |
| `docs/ad-workbench-operations.md` | 实际审核流程、旧素材与过期行为 |
| `docs/worklog-20261026-taxonomy.md` | 本工作日志 |

明确不归属本轮的既有工作包括 `README.md`、`collection.py`、`potw_source.py`、`potw_transport.py`、POTW 测试和先前试点材料。`store.py` 是混合文件，提交时需要保留并分辨原有来源配置与本轮审核代码。

## 验证记录

### Python

- 全库最终结果：**659 passed / 9 failed，89.55 秒**。
- 新增分类文件包含 **30 项用例，全部通过**；原审核、发布、安全三个文件共 **60 项，全部通过**。
- 全库中的工作台与知识发布相关用例通过。首次扩大检查发现本机缺 `cssselect` 与 `respx`，随后仅安装到临时测试依赖目录，再完成全库检查，没有修改全局 Python 包。
- 9 个失败均在未修改的模块。使用 `git archive HEAD` 建立一次临时干净快照，原样重跑这 9 项，得到 **9 failed / 27 deselected，8.62 秒**，失败点与当前工作区一致。临时快照随后已清理。

| 已复现的原有失败 | 数量 | 当前环境证据 |
| --- | ---: | --- |
| `test_collection_client_never_retries_write_and_sends_confirmation` | 1 | JSON 序列化空格差异；本机 httpx 0.27.2，项目指定 0.28.1 |
| `test_checked_in_metric_schema_matches_runtime_contract` | 1 | schema 快照差异；本机 Pydantic 2.9.2，项目指定 2.13.5 |
| `test_static_web_connector.py` 中真实 worker 路径 | 7 | 本机没有 Scrapy，worker 返回 runner_crash |

因此不能声称“全库全绿”；上述对照证明这 9 项不是本轮分类改动新增的回归。没有删改这些断言来获得通过。

本轮实际验证命令（仓库根目录、PowerShell）：

```powershell
python -m pip install --no-deps --target "$env:TEMP/omni-taxonomy-test-deps-20261026" cssselect==1.5.0 respx==0.23.1
$env:PYTHONPATH = "$env:TEMP/omni-taxonomy-test-deps-20261026"
python -m pytest tests -q --tb=short
```

单独重跑本轮测试可以使用：

```powershell
python -m pytest tests/test_workbench_taxonomy.py tests/test_workbench_reviewed_analysis.py tests/test_workbench_publishing.py tests/test_workbench_security.py -q --tb=short
```

基线对照选择了 `tests/test_collection_jobs.py`、`tests/test_metric_contract.py`、`tests/test_static_web_connector.py`，使用如下 `-k` 表达式；执行目录必须是从上述 HEAD 导出的临时干净快照：

```text
test_collection_client_never_retries_write_and_sends_confirmation or test_checked_in_metric_schema_matches_runtime_contract or test_real_scrapy_worker_extracts_caches_and_archives_sanitized_html or test_barriers_and_structure_drift_stop_instead_of_bypass or test_404_requires_two_observations_before_tombstone or test_durable_static_web_run_is_idempotent or test_durable_static_web_run_records_robots_pause
```

### 浏览器与构建

在 `frontend` 执行的 production build（TypeScript + Vite）通过；Chrome 对隔离 Python API 完成 **27 项检查**。使用自有合成图片、随机临时账号、临时数据库；不启动采集，不调用模型。不能把这些结果解释为真实广告质量验收或线上接收成功。

```powershell
npm run build
$env:SMOKE_BROWSER_CHANNEL = 'chrome'
node tests/smoke.mjs
```

本地证据路径（被 Git 忽略，不随报告自动分发）：

- `frontend/test-results/summary.json`：27 项检查名与 passed 状态。
- `frontend/test-results/06-taxonomy-review.png`：桌面人工分类。
- `frontend/test-results/07-mobile-taxonomy.png`：390px 只读分类。

`git diff --check` 通过。源码换行可能触发既有 Git CRLF 提示，不是 diff 格式错误。

## build-bundle 只读核查

核查文件为 `artifacts/private/ad-rollout/build-bundle.py`，本轮没有执行它，也没有更新现存 tar 包或 manifest。

脚本从当前工作区读取固定的项目依赖、Dockerfile、compose、来源登记文件，并递归加入 `src/omnisignal` 与 `frontend/dist` 下的文件；只排除 Python 缓存与字节码。**它不查询 Git 提交状态，也不把 `.gitignore` 当成打包白名单。**

仅复用其路径选择逻辑做只读枚举，当前会选中 145 个文件，包括：

- 未跟踪的 `src/omnisignal/workbench/potw_source.py` 和 `potw_transport.py`。
- 已修改的 `collection.py`，以及含既有 POTW 来源条目和本轮分类改动的 `store.py`。
- 本轮新增的 `taxonomy.py` 和当前 `frontend/dist`。

只读检查现存 `workbench-bundle.tar.gz` 的条目：已包含两个 POTW 模块，但**不包含本轮 `taxonomy.py`**。这是现存本地包内容的证据，不代表服务器当前运行版本；本轮没有连接服务器核对部署状态。

### 后续部署的保留策略

1. 先记录目标版本的文件清单与 SHA-256，明确此次需要保留的 POTW 模块、collection 集成和 store 来源配置。只用 `git archive HEAD` 会遗漏这些未提交内容，不能直接替代现有运行代码。
2. 从确认的源码建立单独暂存副本，将选定的 POTW 变更和分类变更完整带入；不重置工作区，不删除未跟踪文件，不将全部脏目录默认视为已审核内容。
3. 在该副本重建前端，再运行审核、发布、POTW 和浏览器回归。现脚本会直接打入已有 `frontend/dist`，若不重建可能出现前后端版本不一致。
4. 核对产物清单只含预期源码、依赖和前端；脚本当前不是秘密扫描器，不能因为排除了顶层 data/.env 就认为递归目录内的任意文件都安全。
5. 保存既有镜像和数据备份，用新镜像标签切换，失败时回退镜像。保留宿主机工作台数据、发布目录及单独挂载的签名私钥，不通过清空数据库来“修复”版本切换。

以上是待部署时采用的策略；本轮没有生成部署产物、覆盖运行目录或操作服务器。
