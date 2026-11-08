# 三站浏览器运行候选与交接 · 2026-11-08

状态：离线工程候选，尚未部署，尚未获得真实站点采集验收。本文不改变现有每日采集上限 500、公司共享库 492 条基线、自动发布与接收余量门禁。

## 主线与范围

本次准备花瓣、站酷、Behance 三个各自独立的浏览器来源，复用现有工作台计划、领取任务、请求预算、停止控制、存储、去重和审计路径。主工作台仍是唯一采集任务所有者，不另起一个同时领取相同计划的 sidecar worker。

**首版只发现真实项目链接并提取项目自己的 OpenGraph 主图，不是完整项目 gallery。** 不抓推荐区冒充项目图，不猜缩略图原图 URL，不猜分页接口或 CDN 域名。项目多图、动态接口和持续增量覆盖仍需各站正常页面证据与后续实现。

本次候选不解决 Pinterest 当前 robots 阻断，也不补 Pexels Key。花瓣已保存响应含 EdgeOne 挑战结构，不能当作普通 SPA 仅需 JavaScript。站酷验证码及 Behance 403 也不能靠切浏览器忽略。正常人工浏览器是否可访问必须另行验证。

## 服务器只读核对

2026-11-08 18:48–18:54 北京时间：

- 生产 `omnisignal-ad-workbench:20261108-snapshot-jpeg-b1b9b797a0cd` 健康，完整提交为 `b1b9b797a0cd401cae81dd57ad14fd9a9a5487ad`。采集上限 500、receipt 配置均存在。
- 生产没有浏览器配置或会话挂载，Python Playwright 模块不存在，未配置 PLAYWRIGHT_BROWSERS_PATH。不能仅凭仓库已有 provider 就称浏览器已接通。
- 没有运行中的专用浏览器容器。五个新来源计划均为 0，来源会话状态记录为空。服务器留有 Pinterest 和站酷历史 profile；没有花瓣或 Behance profile。没有读取 Cookie、账号或会话文件内容。
- 旧 `omnisignal-browser-acceptance:20261107-2066f6e646e2` 镜像 ID 为 `sha256:fca6f46e74283682e94704b0465347b32dd2d937a49e56f357cd12f41e73f1bc`；其实际 OCI/source env 仍指向 `dbf93b13168f660feaee2d473cc36d9fa125a08d`，不能把 tag 当成全部源文件指纹。
- 旧 desktop 镜像与验收镜像配置浏览器路径 `/opt/playwright`、用户 omnisignal；此前已完成隔离 sandbox/fixture 验收，但不是此次三站版本的真实验收。本轮只读取镜像元数据，没有启动容器验证或访问站点。

因此不能直接将旧 browser 镜像替换目前 b1 生产镜像，否则可能丢失 JPEG 修复和本次接线。必须从最终干净提交构建新的不可变 runtime/desktop 镜像，核对完整 SHA 与构建文件清单。旧 profile 是否登录仍未知，不能因为目录存在标记有效。

随后按隔离测试准备要求，另做一次 `network:none`、只读根文件系统、无挂载、cap_drop ALL、no-new-privileges 的一次性 Python 模块探针，没有启动浏览器或访问业务数据：上述 desktop 镜像实际 UID100/GID101，lxml、Pillow、Playwright 可用，pytest 不存在，`/opt/playwright` 存在。它只证明旧镜像可供离线 Chromium 实验，不证明新 provider 已构建到镜像或真实站点可访问。已存在 seccomp 文件为 `/opt/omnisignal/releases/browser-runtime-dbf93b13168f/seccomp-browser-capdrop.json`，SHA256 `1f374d433d7d041eb7c2e3c82e067088b72729bc3b4b9d44203c959a2166c3df`。证据为 `browser-offline-runtime-parameters.json`。

## 配置协议：默认全部关闭

`deploy/browser-config.multi-source.example.json` 是可直接解析的安全默认值：

```json
{"version":1,"sources":{}}
```

当前协议没有 `enabled` 字段；加入一个 `enabled:false` 会成为无效配置，不能拿它表达禁用。默认空 sources、未配置生产 BROWSER_CONFIG、未建立启用计划，才是真正保持关闭。文件不包含 validated，也不应修改来源验收状态来假装通过。

以下是**后续逐站审阅的条目说明，不是本轮启用配置**。三站分别准备专用 profile，使用 `prepare_project_profile(directory, source)` 建立匹配来源标记。不同来源不得复用同一 resolved profile，禁止复制个人浏览器会话。

三站使用新的宿主机根目录 `/opt/omnisignal/data/browser-sessions/project-v1`，将 `OMNISIGNAL_BROWSER_SESSIONS` 指向这个新根；容器挂载目标仍统一 `/app/browser-sessions`，不在容器路径重复增加 project-v1。例如宿主机 `/opt/omnisignal/data/browser-sessions/project-v1/zcool` 对应容器 `/app/browser-sessions/zcool`。人工入口的单来源挂载同样使用 `${OMNISIGNAL_BROWSER_SESSIONS}/${source}` → `/app/browser-sessions/${source}`，因此两个入口指向同一个新的专用目录。

新 project-v1 profile 的 prepare/login 按 source 绑定；它与旧 Pinterest-purpose profile 不自动兼容。保留服务器现有站酷目录及原会话，不改旧 marker、不迁移或复制 Cookie、不把旧目录重命名成新目录。后续正常人工登录在新目录完成，本轮仅说明路径，没有创建服务器 profile。

| 来源 | 容器独立 profile | 公开入口 | 默认滚动 | 请求/时间预算 |
| --- | --- | --- | --- | --- |
| huaban | `/app/browser-sessions/huaban` | `https://huaban.com/` | 0 | 120次 / 120秒 |
| zcool | `/app/browser-sessions/zcool` | `https://www.zcool.com.cn/` | 0 | 120次 / 120秒 |
| behance | `/app/browser-sessions/behance` | `https://www.behance.net/` | 0 | 120次 / 120秒 |

单站候选字段形状（只有该站通过正常现场验收后才加入 sources）：

```json
{
  "profile_dir": "/app/browser-sessions/huaban",
  "listing_url": "https://huaban.com/",
  "headless": true,
  "max_scrolls": 0,
  "max_requests": 120,
  "max_seconds": 120,
  "resource_paths": []
}
```

来源 key 已定义 source_id，不在配置条目重复添加未支持字段。resource_paths 初始为空，不猜资源域名或接口；此时未必能渲染真实站点，这正是待观测缺口。后续只能依据正常真实页面请求增加已知 SOURCE_CONFIGS 允许范围内的明确路径，并继续通过请求门禁。不得为了“网页能打开”放行所有子请求。

## 运行时与 Compose 候选

现有 `deploy/Dockerfile.browser-workbench` 使用锁定 Python/uv，安装 `--extra browser` 和 Playwright Chromium 及系统依赖，路径 `/opt/playwright`；普通用户运行，要求完整 SOURCE_COMMIT。desktop target 另外包含 Xvfb、Openbox、x11vnc、noVNC、websockify，仅用于人工访问。

现有 `deploy/compose.browser-workbench.yaml` 是工作台 overlay：指定新镜像、浏览器配置只读挂载、专用会话目录可写挂载、匹配数据卷 UID/GID、HOME 临时目录、共享内存和经审核 seccomp。其资源预算为内存1536MiB、CPU1.25、shm256MiB。**这会实际改变运行资源、用户和安全配置，不能声称只是加一条环境变量。** 应与现有 b1 完整 Compose 归一化比较，评估同机 4GiB 内存、模型与下载工作负载，确认不会挤掉现有服务。

现有 `deploy/compose.browser-login.yaml` 是独立临时人工入口，不能合并为生产 workbench 服务：只挂单个来源 profile、监听127.0.0.1:6080、最长1200秒、restart=no，不挂工作台数据库、模型凭据或Docker socket。通过现有正常SSH隧道访问，不公开远程桌面。人工窗口和采集 worker 必须互斥占用该来源 profile，退出时正常落盘，不删除锁文件抢占。

浏览器 seccomp 使用此前已验证的受限派生文件方案；保留 cap_drop ALL、no-new-privileges 和正常 Chromium sandbox。不得使用 no-sandbox、全特权、删除访问门禁、伪装其他爬虫身份等方式掩盖失败。

## 最小部署与验收顺序（本轮不执行）

1. 合并本次干净候选代码，相关和全量测试通过后提交。构建与最终完整 SHA 一致的新 runtime/desktop 镜像，核对构建输入和镜像内文件。保留 b1 旧镜像回退路径。
2. 在隔离无网络环境验证 Chromium sandbox、普通用户权限、来源 profile 独占、fixture 列表/项目OG、停止/恢复和去重；不得挂生产数据库或模型凭据。检查部署需要的工具脚本确实复制入镜像，不能假定旧 Dockerfile 自动包含新增脚本。
3. 从现有部署完整 Compose 链生成候选，保留 JPEG 修复、500、v3、receipt、全部业务挂载及来源/用途权限。默认 sources 为空，新站计划关闭。输出精确差异与资源评估，不能直接套旧 Compose 丢掉新配置。
4. 为单个待验站点在宿主机 `browser-sessions/project-v1/{source}` 准备全新专用目录（0700，正确UID/GID、source marker），容器仍使用 `/app/browser-sessions/{source}`。使用新的 source 绑定 prepare/login 入口；保留旧站酷目录，不尝试复用旧 Pinterest-purpose marker。正常人工浏览器确认公开页面或由用户正常登录。遇 robots 拒绝、验证码、403、挑战页时停止并记录，不自动处理或重复撞验证。
5. 正常访问成立后，采集真实列表与项目OG证据，核对原图字节、解码、尺寸和来源对应；为必要资源追加有证据的路径范围。使用隔离数据库与存储做小批采集、再次运行去重、暂停续跑、会话失效测试。完整gallery仍不算完成。
6. 确认采集、AI分析、内部展示和传输用途记录各自具备依据，不把五新站权限自动继承四旧来源。本轮已有自动发布策略只含四旧来源，新站即使采到也不能直接加入公司库。
7. 上线前检查真实队列和冻结发布批次，优雅停止并做一致性数据库、配置和任务快照，保留原图及旧包；有竞争变化就暂停操作。仅按审阅的 Compose 差异切换，验证实际 SHA、健康、挂载、500、receipt 正常鉴权及旧任务计费状态。浏览器来源仍先保持空配置。
8. 逐站加入已验证条目并通过工作台正常操作建立计划；同一时间只启用一个新站。设置明确来源配额，保持全局500，不因新站加入自动提高总量。第一次正式入库需在当前工作台出现来源、来源URL、图片与审计记录；再次运行不重复。该站通过后才继续下一站。
9. 新站经正常分析/质量筛选，且用途与精确接收目标确认后，才可扩展自动发布策略。使用既有 v3/receipt 机制验证 DeepFlare 真正接收，不以临时下载、配置存在或HTTP200代替端到端成功。

## 本轮工程验证

- 当前生产 b1 基线上建立独立 `feat/multi-source-browser-20261108` 分支；原 DSH 未提交工作没有覆盖。
- 补三站 provider、来源配置与调度接线。保守读取当前项目自己的 OG 图片；页内超过单批上限的余项写入持久游标，重启重读核验后继续，不静默丢掉后半页。超过 200 条的单页明确报错，尚不支持无限滚动。
- 人工 prepare/login 与采集器使用同一 source 绑定的 profile 标记；跨站错用目录和旧标记均明确拒绝，保留原文件。旧站酷目录不迁移。
- 锁定依赖环境全量测试 **1406 passed**，171.80 秒；只有既有 FastAPI/Starlette TestClient 弃用提示。新增及既有浏览器相关测试覆盖真实隔离 Store 入库、去重、配额尾部、暂停续跑、验证码/登录停止与来源边界。
- `scripts/project_browser_render_smoke.py` 在服务器现有受限浏览器镜像上通过：网络关闭、根文件系统只读、无业务卷或凭据，只读挂载新 provider 和合成探针。实际 Chromium 对三站合成页面都完成约 1.25–1.39 秒的延迟 DOM 等待、隐藏登录表单不误停、可选图片 403 不误停、项目主图提取与图片字节校验。
- 上述真实 Chromium 实验使用既有镜像加明确只读测试挂载，只证明新模块在该运行环境中的离线逻辑，不是新完整生产镜像部署，也不是三个真实网站采集验收。

### 仍需现场验证

三站的真实正常访问、服务器登录、资源路径范围、首个真实项目OG与当前工作台入库、分页覆盖、持续运行，以及各自素材用途和DeepFlare现场接收。本轮不承诺三站可稳定产出或每天500条可用案例。

只读证据保存在原工作区私有 artifacts 的 `five-source-activation-20261108/browser-wiring-followup.json` 与 `browser-image-metadata.json`。文档不含账号、Cookie或凭据值。
