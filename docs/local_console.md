# OmniSignal 单入口本地总控台

## 打开

双击项目根目录的 `打开OmniSignal总控台.cmd`。启动窗口完成检查后自动关闭，只留下浏览器中的黑蓝总控台。它会依次：

1. 把临时目录与依赖缓存指向 D 盘。
2. 对 `data/omnisignal.db` 执行幂等的 Alembic `upgrade head`；失败时停止，不带病启动。
3. 在 127.0.0.1:8010 启动 FastAPI，并等待数据库 readiness。
4. 在 127.0.0.1:8501 启动 Streamlit，并等待页面健康。
5. 用默认浏览器打开 http://127.0.0.1:8501。

重复双击会检测现有监督进程和页面健康，只重新打开页面，不启动第二套服务。默认仅监听本机回环地址，不对局域网开放。

## 关闭

双击 `关闭OmniSignal总控台.cmd`。关闭脚本读取项目内状态文件，核对 PID 的命令行必须是 `omnisignal.console_launcher` 后才结束。API 和 UI 被放入同一个 Windows Job；监督进程退出时两个子服务一起终止。脚本不会按端口或进程名批量杀其他程序。

如果电脑直接重启或监督进程被强制结束，Windows Job 会清理子服务。旧状态文件可以保留，下一次启动会核对实际 PID，不把陈旧 PID 当成正在运行。

## 文件位置

- 数据库：`data/omnisignal.db`
- 运行状态：`artifacts/private/console/runtime.json`
- 每次启动日志：`artifacts/private/console/logs/<UTC时间-PID>/`
- API 标准输出/错误：`api.stdout.log`、`api.stderr.log`
- 页面标准输出/错误：`ui.stdout.log`、`ui.stderr.log`
- 迁移输出：`migration.stdout.log`、`migration.stderr.log`

这些位置都在 D 盘项目内，并已由 `.gitignore` 排除。状态文件不保存控制令牌或数据库正文。

## 当前总控范围

### 界面填写检索配置

在“采集任务”顶部的“我的检索配置”输入名称（可空）、搜索关键词、自家产品和竞品。标签输入后按回车添加；展开产品条目可填别名和官网域名。首版来源选项为 DuckDuckGo / Brave，每个词每个引擎最多 10 条，最多 10 个词。不填产品时只展示原始检索证据。

点击“保存配置”只保存，不搜索、不调用模型、不创建定时计划。保存后下方出现独立任务卡；确认来源就绪后，再点对应卡片的“运行一次”。名称为空时取首个关键词。可以从已有配置复制修改；同一内容重复保存复用原配置，内容变化另存为独立不可变版本，旧快照仍按旧定义解释。

配置持久化在 D 盘项目的 `data/search_profiles/`，包括内容哈希标识的 `profile.json` 和生成的固定策略 `policy.json`。备份数据时必须同时备份此目录与数据库、原始归档；不要手动修改或删除历史配置。最多 100 份、单份 UTF-8 内容最多 8 KB，首版不提供删除或原地覆盖。API 重启会恢复配置；当前只支持单 API 进程拥有此本地目录。

接口：`GET /ops/search-profiles` 读取配置；`POST /ops/search-profiles` 需要 operator/admin 身份，严格限制关键词、产品、别名、域名及两个引擎，不接收 URL、脚本或文件路径。保存成功追加审计。若磁盘发布已成功但审计数据库暂不可用，接口会报 503；恢复后重存相同内容可补审计，不会重复配置。

采集任务卡现在显示来源检查时间和状态。SearXNG 不可用时按钮禁用；启动 Docker Desktop、运行 `scripts/start-searxng.ps1` 后点击“刷新数据”。本机依赖检查通过并不保证上游搜索成功。

“检索采样”页选择 Web / SearXNG，可查看快照、关键词、引擎、排名、摘要、链接和归档哈希。当前历史示例未绑定实体集，因此只展示证据。任务绑定版本化实体集后，新快照支持按切片计算样本占比。

本地总控台与主 Compose 的 API 默认共用 8010，日常请选择一个入口。本轮保留本地总控台，旧 `omnisignal-api-1` 已停止；SearXNG 独立容器继续运行。

总控台统一查看运行总览、来源、采集任务、运行记录、多日趋势、检索采样、标准记录、数据质量和审计记录。“采集任务”页可以运行 `config/collection_tasks.yaml` 中已启用的固定任务；当前三项都是示例词配置，结果用于验证链路，不代表你的产品数据。SearXNG 任务还要求独立的本机 8888 服务已就绪。

一键入口每次启动会生成一个只在 API/UI 子进程环境中存在的随机 operator 身份。API 只接收 SHA-256，UI 用明文向本机 API 提交采集请求；明文不写数据库、状态文件或日志。这个临时身份只让“采集任务”按钮免粘贴令牌，来源启停仍要求左侧手动输入已有 operator 令牌。

页面不会直接启动爬虫，也不能提交 shell 命令、Python 模块或任意策略路径。请求只包含固定 `task_id`，FastAPI 再检查认证、任务白名单、中央来源登记和来源停用状态，然后交给单工作队列复用 durable runner。排队、运行、成功、暂停和失败均写入数据库；API 重启会把遗留 pending/running 标记为 `api_restarted`，不把失联任务假装成仍在运行。

## 当前定时快照状态

### 采集完成与覆盖状态

新 SearXNG 任务的 `result_summary` 包含 `coverage_status`、`warnings`、`diagnostics`。任务 `status` 仍描述执行生命周期，覆盖状态独立表达 complete/partial/failed/unknown；页面将 partial 显示为“部分成功”。逐引擎表保存每个关键词的结果数、complete/empty/partial/failed 和安全原因分类，不回显上游错误原文。正常零结果（empty）不等于验证码/超时失败。旧任务没有诊断时显示“完成（覆盖未核验）”，不伪造历史原因。

2026-09-08 对上一次笔记软件验收日志的只读核验：DuckDuckGo 在 05:24:37/43/45 UTC 均报 CAPTCHA，Brave 返回 30 条。遇验证码不自动重试或绕过；本次升级未重新采集。

调度能力已经接到同一白名单任务和持久化队列，但当前不会运行：`config/snapshot_schedules.yaml` 的 `auto_start` 为 `false`，每条计划的 `status` 都是 `paused`，启动环境也没有设置 `OMNISIGNAL_SCHEDULER_ENABLED=true`。三层门禁必须同时开启并重启 API，调度线程才会启动；当前没有注册 Windows 计划任务，也不会产生后台采集或模型调用。

关键词和实体定义分别保存在 `config/keyword_sets.yaml` 与 `config/entity_sets.yaml`。`config/collection_tasks.yaml` 只引用固定的 ID/版本，启动时会核对策略 YAML 中的关键词、别名和账号 ID；任一处漂移会安全拒绝启动。新采集记录会保存版本化观测上下文，旧记录在趋势页明确标为 legacy，不伪造版本。

如果 8010 或 8501 已被其他程序占用，启动会停止并在窗口显示日志目录，不会结束占用端口的程序。启动失败时先查看 `runtime.json` 的安全错误和对应日志。

## 命令行备用入口

```powershell
.\scripts\start-console.ps1
.\scripts\stop-console.ps1
```

测试其他端口时可以传 `-ApiPort` 和 `-UiPort`；自动化验收可加 `-NoBrowser`。日常双击入口使用固定默认端口，避免书签和 API 地址漂移。
