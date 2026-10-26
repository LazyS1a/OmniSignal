# 广告工作台运行与验收

此分支的入口为 `/workbench/`，三个主入口是素材库、待处理和采集。原版控制台继续保留为独立程序。工作台使用自己的 SQLite 数据库；旧准备区通过只读迁入，不修改原始图片、案例修订或审核历史。

## 本地运行

在仓库根目录同步 Python 3.12 和固定依赖：

```powershell
uv sync --frozen --group dev
cd frontend
npm ci
npm run build
cd ..
$env:PYTHONPATH = 'src'
uv run python -m omnisignal.workbench init --root data/ad-workbench --username admin
.\scripts\start-ad-workbench.ps1 -EnableWorker
```

打开 `http://127.0.0.1:8503/workbench/`。初始化只打印私有登录文件的位置，不在终端打印密码。重复初始化已有用户名会拒绝覆盖。可另用 `--role viewer` 或 `--role operator` 创建不同角色账号；账号文件仅保存密码派生值。不要将 `data/` 加入 Git。

默认只监听回环地址，适合本机与 SSH 隧道。外网部署应使用 HTTPS，并设 `OMNISIGNAL_WORKBENCH_SECURE_COOKIE=true`。代理应保留真实 Host；单进程运行，不能使用多个 Uvicorn worker 共同启动采集执行器。

## 日常流程

1. 新库为空时，选择导入素材或创建采集计划。测试示例不自动填充真实素材库。
2. 创建计划默认为暂停。检查来源和每日期望请求上限，恢复后才调度；暂停阻止下一次请求，已发出请求允许收尾。
3. 所有导入与自动获取图片进入待处理。人工判断为广告，选择品牌广告或促销与转化，再收录；明确为设计参考的素材也可收录，不要求广告用途。尚不能判断的素材保留待处理或补充参考。
4. 可选图文模型只提供建议。管理员先记录图片分析用途依据，再由操作员提交单图分析；建议不会自动审核或覆盖人工分类。
5. 在详情“发布分类”中核对行业和媒介（各至少一项），设计任务可不选；勾选确认后随收录或保存提交。分类使用现有 `nova-1` 字典，“其他行业”必须说明依据；数字界面仅指网页和界面设计，不能因为文件是一张图片就选择它。
6. 管理员核对展示与导出用途并记录依据，明确选择素材发布；未知权限、缺少分类或已过期审核不能发布。发布版本由 Ed25519 签名，接收状态默认“未确认”，直到消费方真正验收。

无权限时不应绕过角色。viewer 读取，operator 导入、审核与计划控制，admin 还可核对用途、迁入旧库和发布。

分类审核和人工设计分析都绑定素材修订号及图片 hash。普通编辑会保留旧内容，但使旧审核过期；需要再次核对后显式确认。若只提交分类，已存在的设计分析也会因修订号变化而过期，发布前应一起复核，系统不会静默把旧分析当成新版本结论。

旧素材仍可查看、编辑，但不会自动补上“其他行业”或“数字界面”。旧签名包保持原样可读；其中 `editorial.legacy_medium_fallback` 代表历史兼容值，消费端不能把它作为真实分类或筛选命中。新包的 `case.tags` 来自当前人工审核，具体字段见 [知识发布协议](../src/omnisignal/knowledge/PROTOCOL.md)。

## 状态与预算

- 配置状态、运行开关、上次运行分别展示。“配置就绪”不是连通性证明，“无本地记录”不是其他采集器从未运行。
- 每次 HTTP 请求含 robots、图片、失败请求都先原子预留一次额度。按 UTC 日重置；请求预算不是费用账单。
- 同计划不会同时运行两次；重启将遗留运行标记为中断，保留下一次检查时间和已用额度。
- 来源返回拒绝访问、验证码、非公开地址或超出允许域名时停止，不扩大范围或绕过限制。
- 旧研究来源和合成夹具放设置诊断，标注为独立来源；它们不会因进入广告工作台就自动开始采集。

## 独立模型配置

仅工作台服务端读取以下环境变量，不复制其他产品的账号：

```text
OMNISIGNAL_WORKBENCH_MODEL_API_KEY
OMNISIGNAL_WORKBENCH_MODEL
OMNISIGNAL_WORKBENCH_MODEL_BASE_URL
OMNISIGNAL_WORKBENCH_MODEL_DAILY_REQUEST_LIMIT
```

每日额度默认为 0，关闭付费分析。提交图片缩小至最长边 1024 像素，仅附带所选资料的限长说明。失败可能已产生外部费用，仍消耗额度，必须明确重试；中断也不会自动重复计费。金额以提供方账单为准，界面只统计请求次数。

## 旧库迁移与下游读取

配置 `OMNISIGNAL_PREPARATION_DIR` 指向已有准备区，管理员在设置中执行迁入。SQLite 用只读模式打开；逐图校验原文件 hash，新工作台全部先进入待处理，旧审批不等同广告分类。重复迁入不覆盖新工作台的人工修订。

发布配置：`OMNISIGNAL_KNOWLEDGE_DIR`、`OMNISIGNAL_KNOWLEDGE_SIGNING_KEY_FILE`、可选 `OMNISIGNAL_KNOWLEDGE_KEY_ID`。私钥必须在发布目录之外。

独立下游身份使用 `OMNISIGNAL_KNOWLEDGE_READ_TOKEN_SHA256`。下游持有对应原始令牌，调用 `/knowledge/v1/latest`、版本 manifest 和清单内文件；浏览器会话不能代替下游身份。协议仍为 schema 1 / nova-1，参考接收器可校验签名、图片与完整性后切换版本。具体部署接通和接收回执必须另行取得真实证据。

## 备份与恢复

```powershell
uv run python -m omnisignal.workbench backup --root data/ad-workbench --destination backups/ad-workbench-snapshot
uv run python -m omnisignal.workbench restore --root backups/ad-workbench-snapshot --destination data/ad-workbench-restored
```

备份包含工作台数据库、图片、本地账号散列文件与配置指向的发布包/永久撤回记录；**签名私钥和外部账号文件需要单独备份**。发布内容统一恢复到新目录下的 `published/`，切换前应核对 `OMNISIGNAL_KNOWLEDGE_DIR`，避免继续指向旧外置目录。恢复只允许新目录，校验备份 hash 后复制，全部计划暂停，原会话失效。切换前先验证恢复库，再调整服务数据挂载；原库保留用于回退。

## 浏览器回归

在 frontend 目录执行 `npm run test:smoke`。默认创建隔离的临时 Python API、SQLite、随机测试账号和标注为合成的图片；不启动采集或模型任务，退出清理临时数据。`SMOKE_PYTHON` 指定安装好项目依赖的 Python；已安装 Chrome 时可设 `SMOKE_BROWSER_CHANNEL=chrome`，否则先执行 `npx playwright install chromium`。禁止将外部服务模式指向生产库。

容器配置见 `deploy/Dockerfile.workbench` 和 `deploy/compose.workbench.yaml`。数据目录的宿主机权限需与容器用户匹配；账号、原库和发布根目录不进入镜像。原库只读挂载。镜像切换失败时停止新工作台，恢复之前入口；不要删除旧数据。
