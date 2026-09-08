# 运维 API

Gate 7A--7D 提供给 Streamlit 运维台使用的有界查询、来源控制和标准记录导出层。界面和其他调用方不得绕过它直接访问数据库。

## 接口

- `GET /ops/summary?window_hours=24`：运行窗口、采集运行数、记录数、插件尝试数、质量状态与原始归档覆盖率。
- `GET /ops/sources`：中央来源登记状态、速率预算、授权到期时间、kill switch、检查点版本和最近一次采集运行。
- `GET /ops/runs?kind=ingestion|normalization|plugin`：三类运行历史。可按 `source_id`、`status` 筛选。
- `GET /ops/runs/{kind}/{run_id}`：单次运行详情；标准化运行额外返回成员覆盖率。
- `GET /ops/records`：按来源、质量状态、文本、标准化 run 或插件 run 查询标准记录。
- `GET /ops/records/{normalized_id}`：标准内容、质量事件、运行成员、重复候选、上下文边和插件输出。
- `GET /ops/exports/records.csv`：按来源、质量状态和关键词导出最多 1000 条标准记录 CSV。
- `GET /ops/quality`：质量状态占比和质量事件代码计数。
- `GET /ops/audit`：按 action/target 查询操作审计。
- `GET /ops/control/whoami`：验证当前 Bearer token，返回 actor 与 role；控制面未配置时返回 503。
- `POST /ops/sources/{source_id}/control`：启用或停用后续新运行，仅 operator/admin 可用。
- `GET /ops/collection-tasks`：返回服务端固定任务白名单，不返回策略文件内容或路径。
- `GET /ops/snapshot-schedules`：返回全局调度门禁、计划状态、间隔和下个窗口预览；当前全部暂停。
- `GET /ops/collection-jobs?limit=20`：查看最多 100 条人工触发任务及其安全计数摘要。
- `GET /ops/collection-jobs/{job_id}`：查看一条任务的排队、运行、成功、暂停或失败状态。
- `POST /ops/collection-jobs`：提交一个白名单任务，仅 operator/admin 可用，返回 202。
- `GET /ops/trends?days=90&metric_type=all&q=`：读取相对兴趣、联想排名、视频样本命中数或样本占比序列；保留各自单位、范围、分子/分母和配置版本。

所有列表使用 `limit` 和 `offset`，`limit` 范围为 1--200，offset 最大 100000；排序包含稳定 ID，避免同一页随机变化。记录列表只返回 240 字预览，完整正文仅在单条详情返回。

## 数据口径

每个占比同时返回：

- `numerator`：分子；
- `denominator`：分母；
- `ratio`：0--1 的比例，分母为 0 时返回 null；
- `scope`：覆盖的数据集合。

质量状态只表示数据可用性，重复分组只表示技术候选。接口不提供产品评分、情绪结论或改进建议。

## 安全边界

- `/ops/sources` 不返回 checkpoint JSON，只返回版本和更新时间。
- 采集运行不返回自由文本错误正文，只返回错误分类码。
- 审计和插件 JSON 会递归屏蔽 token、secret、password、cookie、private key、Authorization 及本机绝对路径。
- API 仅在显式配置控制 principal 时开放来源启停；默认未配置时控制面返回 503。
- 来源控制只改变数据库 operational override，不改写中央 source registry；停用阻止后续新运行，不强杀已在途进程。
- CSV 导出不提供用户文件名、服务器路径、原始归档正文、checkpoint 或审计 detail；固定 `no-store` 与 `nosniff` 响应头。
- 删除、配置修改、凭证管理和任意 SQL 仍没有接口。
- 采集提交只接受固定 `task_id` 和精确 `RUN <task_id>` 确认短语；不接受命令、模块名、URL 或策略路径。任务仍会经过来源登记和 operational kill switch。

## 采集任务契约

任务定义位于 `config/collection_tasks.yaml`，当前登记公开趋势/联想、YouTube 检索和本机 SearXNG 三条示例词任务。提交请求需要 Bearer token、16--128 位 `Idempotency-Key`，以及 `task_id` 和精确确认短语。同 actor、同幂等键、同载荷返回原任务；同键异载荷返回 409。队列最多保留 20 条 pending/running，单 API 进程只并行执行 1 条。SearXNG 新任务在入队前强制复查本机依赖；不可用返回 503 和稳定错误码，不创建 job。已存在任务的幂等重放仍返回原任务。

任务列表包含 `readiness`（status、available、detail_code、message、checked_at）。SearXNG 探测固定本机 `/config` 与已启用引擎，读取过程最多 512000 字节，HTTP 超时为 2 秒，展示检查缓存 15 秒。此检查不保证外部搜索引擎可达或搜索 JSON 输出已启用；外部故障仍由采集器处理。其余任务的 configured 状态仅表示配置就绪。

`GET /ops/web-visibility` 与 `GET /ops/web-visibility/{archive_sha256}` 提供 SearXNG 快照和逐条证据。列表支持 q、limit（1--50）、offset；筛选关键词选择整个快照。每个 query/engine 切片单独计数，实体匹配使用标题、摘要和已配置域名。缺少实体配置不输出品牌比例；不完整切片的比例为 null。现有 YouTube `/ops/visibility` 契约保持独立。

任务生命周期写入 `collection_jobs`；连接器运行仍写 `ingestion_runs`，两者共享预分配 `run_id`。响应结果只包含批次、读取、新增、更新、未变、墓碑和周期完成计数，不返回原始响应。API 重启时，旧 pending/running 会收口为 failed/`api_restarted` 并追加审计，是否重跑由人刷新证据后决定，不自动重复外部请求。

任务同时引用 `config/keyword_sets.yaml` 和可选的 `config/entity_sets.yaml` 固定版本，并登记 `access_tier` 与 `observation_scope`。服务启动会把任务策略与目录内容逐项核对；未知版本或关键词/实体漂移会拒绝启动。通过任务执行的新记录带 `observation_context` 与定义哈希；历史旧记录仍可读，但会标记为缺少版本化上下文。

调度定义位于 `config/snapshot_schedules.yaml`。只有环境变量 `OMNISIGNAL_SCHEDULER_ENABLED=true`、文件总开关 `auto_start: true` 和单计划 `status: active` 三者同时成立，API 生命周期才启动调度线程。调度按锚点时间槽生成幂等 command hash，仍复用 20 条活动上限、来源停用门禁、单 worker 和 `collection_jobs`；错过多个历史窗口只补当前槽，不突发回放。目前三层门禁均未打开。

## 来源控制契约

控制面默认关闭。API 只读取 `OMNISIGNAL_CONTROL_PRINCIPALS_JSON` 中的 actor、role 和 token SHA-256；明文 token 不进入 API 配置、数据库、日志或审计。可用项目脚本生成一个本地 principal：

```powershell
.\scripts\new-control-principal.ps1
```

把脚本输出的两行复制到启动 API 的同一个 PowerShell：第一行是 API 使用的摘要配置，第二行明文 token 只用于粘贴到 Streamlit。不要把两行写进 `.env`、README、截图或仓库；API 启动后可执行 `Remove-Item Env:OMNISIGNAL_CONTROL_TOKEN`。

写请求必须同时提供：

- `Authorization: Bearer <token>`；
- `Idempotency-Key`：16--128 位调用方唯一值；
- `expected_version`：来自 `/ops/sources` 的 `control_version`；
- `confirmation`：精确等于 `ENABLE <source_id>` 或 `DISABLE <source_id>`；
- `reason`：3--240 字符，禁止放 token、密码、Cookie 或其他秘密。

同 actor、同 idempotency key、同载荷会返回第一次结果且不重复写审计；同 key 异载荷、旧版本或并发首写返回 409。每次首次成功变更写一条 `source_control_changed` AuditEvent，只保存 actor、role、前后状态、控制版本、原因摘要和幂等键摘要。

## 标准记录 CSV

`GET /ops/exports/records.csv` 复用记录查询的 `source_id`、`quality_status` 和 `q` 筛选，`limit` 范围为 1--1000，默认 1000。记录按创建时间和 normalized id 稳定倒序；空结果仍返回只有表头的合法 UTF-8 BOM CSV。

每行包括来源 ID、来源记录 ID、source schema、normalizer version、config hash、normalized hash、质量状态、权限和 raw archive SHA，以及受限的 URL、标题、正文和时间字段。URL 最多 2048 字、标题最多 500 字、正文最多 4000 字，三个 `*_truncated` 字段明确标记截断。以 `= + - @` 开头（含前导空白）的文本会加单引号，避免电子表格把采集文本当公式执行。

响应头 `X-OmniSignal-Export-Rows` 给出实际行数，文件名固定为 `omnisignal-records.csv`。导出在内存中生成并直接返回，不落服务器临时文件；Streamlit client 另设 10 MiB 上限。若数据库不可用，接口返回安全的 503，不返回半个文件或数据库路径。

## Streamlit 运维台

运维台已实现为九个页面：总览、来源、采集任务、运行、多日趋势、检索采样、标准记录、质量和审计。页面只调用上述 HTTP 接口，数据库连接、查询口径和脱敏继续留在 FastAPI；来源页提供受控启停，采集任务页只运行固定白名单，标准记录页提供有界 CSV，其余功能仍为只读。

多日趋势页不会跨单位叠图：Google Trends 是请求内归一化的 0--100 指数，Autocomplete 是排名，YouTube 可见度是一次有效视频样本中的计数或比例。API 明确返回 `absolute_search_volume_available=false` 和 `cross_platform_global_share_available=false`；未来接入合法绝对量来源时必须新增独立适配器，不能把现有公开信号改名冒充。

启动 API 后，在另一个 PowerShell 运行：

```powershell
.\scripts\start-ops-ui.ps1
```

默认地址为 `http://127.0.0.1:8501`，API 默认地址为 `http://127.0.0.1:8010`。可用 `OMNISIGNAL_UI_PORT` 和 `OMNISIGNAL_API_BASE_URL` 覆盖；API base URL 只接受不带账号、路径、查询参数或 fragment 的 HTTP(S) origin。

页面缓存固定为 15 秒，人工刷新会清空缓存。客户端只允许 `/ops/*`，限制查询分页、窗口、响应体大小和重试次数；错误信息不回显上游响应正文。API 不可用、响应异常和空数据都显式展示，不填充模拟数据。

标准记录页的“准备当前筛选 CSV”只在点击时请求导出，导出不进入 15 秒 JSON 缓存。准备成功后才出现下载按钮，并显示实际行数与截断口径；失败时不会生成占位文件。

运维台默认只绑定 `127.0.0.1`。输入 token 后，只有 API 返回 operator/admin 身份时来源页才显示控制表单；这只是交互便利，真正权限仍由 FastAPI 执行。若以后暴露到局域网或公网，还需单独评审反向代理、TLS、OIDC、CSRF 和浏览器会话，不应把静态 Bearer token 当公网登录方案。

## Gate 8：未结束运行观测

`GET /ops/summary` 增加 `unfinished_ingestion`：`total` 是全部历史中 pending/running 记录数，`needs_review` 是其中创建超过 24 小时的数量，`review_after_hours=24`、`scope=all_history`。独立于 `window_hours`，避免旧的孤立记录从最近窗口消失。仅提供观测，不改状态，不代表 worker 已死亡，不自动重启任务。
