# 服务器专用浏览器登录记录（2026-11-06）

## 当前结论

已在 Omni 服务器准备独立、临时的浏览器登录入口，并通过 SSH 隧道提供本机访问。完整 Chromium 的隔离启动与 noVNC 通道已实测通过。用户已确认远程浏览器能打开，当前需要在其中打开目标网站；截至本文记录时，**站酷登录待完成，目标页面和后续正式采集待验收**。

本次没有采集图片、调用模型、切换 Omni 主服务版本，也没有调整每日 500 张目标或素材发布权限。浏览器登录环境可用，不代表五个新增来源已具备持续采集能力。

本文汇总本聊天部署执行方返回的服务器实测结果，并对照本地登录脚本与 Compose 文件记录恢复步骤；文档编写过程没有再次连接服务器或执行恢复命令。

## 实际部署记录

| 项目 | 本次记录 |
| --- | --- |
| 服务器连接 | 正常 SSH 已恢复 |
| 浏览器桌面镜像对应源码提交 | `dbf93b13168f660feaee2d473cc36d9fa125a08d` |
| 已构建镜像 ID | `sha256:f9ec1a790165a387ebcedfbfe508525b605d68cdf28aa2dbe92fd864a64f8632` |
| Omni 主服务版本 | 仍为 `6266fcdd`，未切换至本次浏览器代码 |
| Compose 项目 | `omni-browser-login` |
| 实际临时容器名称 | `omni-browser-login-zcool` |
| 启动方式 | `docker compose run --rm --service-ports --name ... browser-login`，交互终端保持连接 |
| 容器用户 | UID `100`、GID `101`，非 root |
| 权限与文件系统 | `cap_drop: ALL`、`read_only: true`、`no-new-privileges: true` |
| 资源边界 | 内存 1536 MiB、CPU 1.0、共享内存 256 MiB、临时目录 256 MiB |
| 服务器监听 | `127.0.0.1:6080`，未对公网开放桌面端口 |
| 本机转发 | `127.0.0.1:16080` → 服务器 `127.0.0.1:6080` |
| 本次专用持久会话目录 | `/opt/omnisignal/data/browser-sessions/zcool`，权限 `0700` |
| 持久目录挂载 | 仅本次来源的专用目录；未挂载个人浏览器目录或其他来源会话 |
| 发布目录 | `/opt/omnisignal/releases/browser-runtime-dbf93b13168f` |
| 运行配置文件 | 上述发布目录内的 `browser-login.env`；本文不记录其内容 |
| Compose 文件 | 上述发布目录内的 `deploy/compose.browser-login.yaml` |
| 登录窗口时长 | 本次启动参数为 `--max-seconds 1200`，最多 20 分钟 |

容器使用 `--rm`，退出后自动删除容器实例；已挂载的专用会话目录保留。它不是常驻采集服务，也不应通过 `docker start` 恢复一个已删除的实例。

## 已完成的真实验证

- 在 `network: none` 的隔离验收环境中，使用**完整 Chromium** 验证了实际 sandbox 页面，页面明确显示 `adequately sandboxed`，并成功完成渲染。
- 验收进程 `CapEff`、`CapBnd` 均为零，`NoNewPrivs=1`、`Seccomp=2`；普通非 root 进程直接执行 `chroot` 被拒绝。
- noVNC 本机入口返回 HTTP `200`，WebSocket 升级返回 `101`，RFB 握手返回 `003.008`。
- 当次观察到桌面容器处于 running 状态，没有 OOM。

这里的 `network: none` 专指隔离启动验收；正常访问目标网站的人工登录窗口需要网络。两种运行场景不能混写为同一次验证。

旧镜像内原 smoke 脚本使用 headless shell 打开 `chrome://sandbox`，返回 `ERR_INVALID_URL`，因此**原脚本不能记为验收通过**。后续已将修正版脚本作为运维 probe，以单文件只读挂载方式放入上述 `dbf93b` 镜像验收；在 `network: none` 且没有挂载真实用户 profile 的条件下，默认 headless shell 渲染和完整 Chromium sandbox 检查均已通过。此次不是镜像内原脚本自动变更或通过。

这轮 probe 报告 Playwright `1.62.0`、headless shell `151.0.7922.34`、完整 Chromium UA `151.0.0.0`，采集与模型调用均为零。本地 smoke verdict 与人工登录入口回归共 `34 passed`。该测试结果证明浏览器运行条件，不替代真实站点页面验收。

## seccomp 兼容修正

Playwright 官方固定版本 profile 保留为原始依赖；实际使用的派生文件为：

`/opt/omnisignal/releases/browser-runtime-dbf93b13168f/seccomp-browser-capdrop.json`

派生文件 SHA256：

`1f374d433d7d041eb7c2e3c82e067088b72729bc3b4b9d44203c959a2166c3df`

兼容变化仅为添加 `chroot` syscall 的显式允许规则，以适配 Chromium 子 user namespace 中的文件系统沙箱。没有给容器增加 capability；默认拒绝策略及其他隔离条件继续保留。原 profile 的 `chroot` 规则受 `CAP_SYS_CHROOT` 条件约束，与 `cap_drop: ALL` 组合时会被过滤。

这项调整允许调用进入内核正常权限检查，并不赋予普通容器进程 `chroot` 权限；上面的实际拒绝测试已覆盖这一点。它也不能把 syscall 的路径参数限制为一个特定字符串。原始依赖来源为 [Playwright v1.62.0 官方 seccomp profile](https://raw.githubusercontent.com/microsoft/playwright/v1.62.0/utils/docker/seccomp_profile.json)。

## 用户入口与下一步

SSH 隧道和临时桌面会话都在运行时，在**建立隧道的同一台电脑**打开：

[服务器专用浏览器登录入口](http://127.0.0.1:16080/vnc.html?autoconnect=true&resize=scale)

这里显示的是服务器专用浏览器。用户在页面中正常完成站点登录；密码、验证码及浏览器会话内容不需要发送到聊天，也不应写进本仓库。此 `127.0.0.1` 地址不能直接分享给另一台设备使用。

本次目标是站酷：在**远程桌面内的 Chromium 地址栏**打开 [https://www.zcool.com.cn/](https://www.zcool.com.cn/)，随后自行正常登录。浏览器桌面入口和素材网站地址是两层不同入口；只打开 noVNC 桌面，并不等于已经打开站酷页面。

下一步按以下顺序推进：

1. 在远程 Chromium 中打开站酷目标地址，确认实际页面状态。用户已澄清此前问题是目标网站入口缺失，不是确认遭到拦截；当前不能标记为 HTTP 403 或访问受阻。
2. 由用户完成正常人工登录；若后续实际出现验证码，由用户自行处理。若页面明确拒绝访问，保留实际原因并停止该来源，不自动换通道绕过。
3. 页面正常访问后，再验收对应来源的列表、详情与真实图片地址，检查尺寸、解码、来源关联和重复过滤。
4. 登录结束时，通过交互终端正常退出浏览器，使会话完整落盘。同一来源的持久 profile 不得同时被人工浏览器和采集 worker 占用。
5. 完成适配器与单个来源的小批量入库验收，确认重跑去重、暂停、续跑和失效状态，再讨论纳入正式采集计划。

五个来源的独立 profile 人工入口已有代码支持；**本次代码中的持久采集 provider 只有 Pinterest**，其他来源仍需各自适配。Omni 主服务尚未切换，不能据此认定线上 worker 已启用这套 provider。Pinterest 的 Pin 路径当前 robots 检查为拒绝；其余四个来源仅通过此前 robots 检查，没有完成页面和采集验收。robots 放行不等于页面可用，也不等于图片已入库。

## 恢复与停止命令

下列为人工运维步骤，本文没有执行。`<现有SSH别名>` 是占位符，替换成当前已配置、正常可用的 SSH 别名；不需要在命令中填写密码或私钥内容。

### 1. 在本机恢复隧道

另开一个 PowerShell 窗口，保持前台运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:16080:127.0.0.1:6080 <现有SSH别名>
```

若本机 16080 已被现有隧道占用，先确认该隧道是否可直接使用，不要重复启动。如果只是关闭了本机隧道窗口，服务器上的临时桌面未必已经结束。

### 2. 检查服务器的临时容器

使用正常 SSH 交互连接；需要保持 TTY：

```powershell
ssh -tt <现有SSH别名>
```

在服务器终端中只读查看指定容器：

```bash
sudo docker ps -a --filter name=omni-browser-login-zcool --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

如果目标容器仍在运行，优先恢复隧道后继续使用。若已超过 20 分钟退出且容器被自动删除，再执行下面的重建命令。不要同时运行两个占用同一站酷 profile 的浏览器实例。

### 3. 重建一次临时登录窗口

在上述服务器交互终端执行并保持终端打开：

```bash
sudo docker compose \
  --env-file /opt/omnisignal/releases/browser-runtime-dbf93b13168f/browser-login.env \
  -p omni-browser-login \
  -f /opt/omnisignal/releases/browser-runtime-dbf93b13168f/deploy/compose.browser-login.yaml \
  run --rm --service-ports --name omni-browser-login-zcool browser-login
```

该命令复用本次已核验的配置与专用会话目录；不切换 Omni 主服务，不清空登录状态，不启动采集或模型任务。出现同名容器、profile 占用或端口冲突时，先检查实际运行实例，不自动删除目录或锁文件。

### 4. 结束人工窗口

正常完成登录后，按脚本提示在该交互终端按 Enter 退出。需要中途停止时，在对应交互终端按 Ctrl+C；若终端已断开，可以仅停止指定临时容器：

```bash
sudo docker stop omni-browser-login-zcool
```

`--rm` 会随后清理容器实例，站酷专用 profile 仍保留。隧道窗口中的 Ctrl+C 只结束本机 SSH 转发。不要对 Omni 主服务执行整栈 `down`，不要删除 `/opt/omnisignal/data/browser-sessions/zcool`。

## 本次交付边界

本次交付是可供用户操作的服务器专用浏览器入口，以及经过实际验证的 Chromium 隔离环境。尚未交付五站持续采集，也未证明每日 500 张实际产能。后续来源适配、受阻页面处理、正式入库和主服务切换分别需要真实验收；素材分析、展示、传输与发布仍遵循各自现有授权记录。
