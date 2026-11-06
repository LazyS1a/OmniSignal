# 浏览器运行镜像与服务器专用登录入口

日期：2026-11-06。状态：本地实现与离线校验完成；**未构建镜像、未启动 Chromium、未启动登录服务、未切换现网**。

## 本轮交付

- `deploy/Dockerfile.browser-workbench`：Debian Bookworm / Python 3.12、固定镜像 digest、固定 uv 0.12.8，严格按 `uv.lock` 安装 browser extra（Playwright 1.62.0）及其对应 Chromium。`runtime` 是采集工作台；`desktop` 是仅供正常人工登录的临时桌面。
- 构建必须提供完整 40 位小写 Git SHA，不能以 `uncommitted` 充当交付版本。构建 UID/GID 必须与已有数据/专用会话目录的实际所有者一致，不能直接套默认值。Debian 内已有同数值 GID 时复用该组；请求 UID 已被占用则明确构建失败，不改原账户身份。新用户的 UID/GID 创建后再次严格核对。
- `compose.browser-workbench.yaml` 在原部署配置之后合并，保留原业务挂载、任务、额度和授权。新增独立 profile 卷与只读 browser config。默认应先使用 `browser-config.disabled.json`，它不启用任何来源。
- `compose.browser-login.yaml` 独立启动，每次只挂载所选来源的专用会话子目录，不挂其他来源会话、工作台数据库、模型配置、签名密钥或 Docker socket。宿主机只监听 `127.0.0.1:6080`；VNC 在容器内仅监听 loopback，入口通过 SSH 本地转发。
- `browser_login_desktop.py` 只启动桌面及既有人工登录脚本，最多运行 20 分钟，结束后清理自己创建的进程组。保持同一个专用目录锁，采集器与人工登录不会同时使用 profile。脚本不读取账号、验证码或 Cookie。
- 登录与隔离 smoke 显式开启 `chromium_sandbox=True`。正式 provider 同样必须开启 sandbox；不允许失败后自动降级为 `--no-sandbox`。

## 必须保留的限制

这是**完整的新运行镜像**，不是昨日 9 模块的原镜像覆盖包。Linux 基础库、Python 补丁版本、浏览器依赖及进程内存均会变化。因此必须重新验证运行文件清单、依赖、原功能、数据备份、worker 暂停启动与兼容性，再恢复调度；不能复用昨日覆盖包检查结论去宣称全部旧运行文件逐字节保全。

Playwright 官方建议对网页采集使用非 root 用户及允许 Chromium 所需 user namespace 的 seccomp profile。本文件未下载该 profile，Compose 要求显式提供已审阅的绝对文件路径，缺失就拒绝配置。不得以 privileged、SYS_ADMIN、seccomp=unconfined 或关闭宿主机 AppArmor 来补过检查。

参考：[Playwright Docker](https://playwright.dev/python/docs/docker)、[系统要求](https://playwright.dev/python/docs/intro#system-requirements)、[官方 seccomp profile](https://github.com/microsoft/playwright/blob/main/utils/docker/seccomp_profile.json)。本轮核对了当前官方说明和基础镜像 registry digest；网络权限撤回后停止所有联网动作，未下载 seccomp 文件、镜像或依赖。

## 网络恢复后由部署任务执行

先只读确认当前容器运行提交、实际 UID/GID、业务挂载/配置、磁盘和内存；取得官方 seccomp 固定版本文件并记录其 SHA256。已有现场样本为磁盘约 31 GiB 可用、内存约 2.3 GiB 可用，不能把这个快照当作后续构建或运行内存保证。

从最终干净源码及已验收 frontend/dist 构建，以下由部署者在 Ubuntu 运维终端执行。既有 SSH 账号访问 Docker socket 需要已验证的 `sudo -n docker` 路径；不要把裸 Docker 命令交给用户碰权限错误。变量须由实际盘点填入，不在聊天里传任何凭据：

```sh
sudo -n docker build --target runtime --file deploy/Dockerfile.browser-workbench \
  --build-arg SOURCE_COMMIT="$SOURCE_SHA" \
  --build-arg OMNISIGNAL_UID="$APP_UID" --build-arg OMNISIGNAL_GID="$APP_GID" \
  --tag "omni-browser:$SOURCE_SHA" .

sudo -n docker build --target desktop --file deploy/Dockerfile.browser-workbench \
  --build-arg SOURCE_COMMIT="$SOURCE_SHA" \
  --build-arg OMNISIGNAL_UID="$APP_UID" --build-arg OMNISIGNAL_GID="$APP_GID" \
  --tag "omni-browser-desktop:$SOURCE_SHA" .

sudo -n docker run --rm --init --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --security-opt "seccomp=$SECCOMP_FILE" \
  --shm-size 256m --memory 1536m --cpus 1 \
  --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777 \
  --entrypoint python "omni-browser:$SOURCE_SHA" /app/scripts/browser_runtime_smoke.py
```

隔离 smoke 只能使用 `--network none`，不得挂已有 profile 或线上数据。它检查非 root、真实 Chromium 渲染、`chrome://sandbox` 显示 adequately sandboxed，以及没有加载 Pinterest 运行配置；失败即停止，不降级安全配置。镜像准备后仍须另外做空白库的原工作台 API/前端和持久化兼容验收。

准备会话目录只创建新空私有目录或使用 Omni 自己标记的目录，设对应用户所有权。**不得递归改现有业务目录所有权，也不得复制个人浏览器目录。** 采集容器挂载会话根目录到 `/app/browser-sessions`，临时桌面仅挂其中所选来源的子目录；两者指向该来源同一份专用会话。配置例子为 `browser-config.pinterest.example.json`。

五站人工登录入口通过 `--source` 白名单选择：`pinterest`、`huaban`、`zcool`、`behance`、`pexels`，省略时仍是 Pinterest。服务器部署环境文件中设置 `OMNISIGNAL_BROWSER_SOURCE` 为其中一个值；Compose 将 `${OMNISIGNAL_BROWSER_SESSIONS}/所选来源` 挂到 `/app/browser-sessions/所选来源`。部署者必须先创建该独立私有子目录，并核对它属于实际 UID/GID 且不是符号链接；`create_host_path: false` 会拒绝隐式创建目录。不同来源不复用同一 Chromium profile。现有 profile 所有权标记保留原格式，以兼容已有专用会话和 provider，标记不代表登录或采集已验收。

落地页只正常打开一次；如果 30 秒加载超时，保留可见浏览器直到用户结束或桌面 20 分钟期限到达，不自动重试、不处理验证页面。启动浏览器失败仍明确退出。这个入口只解决用户正常登录，不代表五站采集适配器或访问条件都已通过验收。

## 用户实际需要做的一次操作

前提：目标来源正常访问规则检查已通过、桌面隔离运行验收已完成。此前香港 Pinterest 目标 Pin 的 robots 检查曾拒绝访问，应先复核；不要让用户白登录，也不能用登录绕过拒绝。

专用桌面配置所需变量：`OMNISIGNAL_BROWSER_DESKTOP_IMAGE`、`OMNISIGNAL_BROWSER_UID`、`OMNISIGNAL_BROWSER_GID`、`OMNISIGNAL_BROWSER_SESSIONS`、`OMNISIGNAL_BROWSER_SECCOMP`，均使用已验证的镜像与绝对路径；可选 `OMNISIGNAL_BROWSER_SOURCE` 默认为 `pinterest`。之后由部署者在已有 Docker 运维权限的服务器交互终端启动并维持该临时会话；用户只操作正常浏览器登录，不自行调整服务器权限：

```sh
sudo -n docker compose --env-file /path/to/browser-runtime.env \
  --file /path/to/compose.browser-login.yaml run --rm --service-ports browser-login
```

Windows 本机另开终端建立现有 SSH 隧道：

```powershell
& D:/Git/usr/bin/ssh.exe -F C:/Users/Administrator/.ssh/omnisignal_hk.conf `
  -o ExitOnForwardFailure=yes -N -L 127.0.0.1:16080:127.0.0.1:6080 omnisignal-cloud
```

用户打开 `http://127.0.0.1:16080/vnc.html`，在服务器的这份新专用浏览器中亲自正常登录，自己完成站点要求的验证；结束后回到交互终端按 Enter，释放 profile 锁。此时只完成登录操作，不能自动记为采集验收成功。不要发送密码、Cookie 或验证码。

部署者须使用实际交互 TTY：SSH 分配 TTY，Compose `run` 保留 stdin，不能使用 `-T`、重定向到 `/dev/null` 或纯后台无人值守调用来代替这次人工会话。终端连接要保持到用户按 Enter。部署者还应在移交前确认 noVNC 的 HTTP 页面可访问且 Xvfb、x11vnc、websockify 均存活；脚本打印地址并不构成这三个组件的独立健康验收。VNC 使用 `-nopw`，因此 host loopback 和 SSH 隧道是必要边界，不能改成公网端口。

采集放行仍需由正常计划的受限 1–3 作品小样验证详情图、身份、尺寸、哈希、去重、暂停和续跑。五源采集、素材用途授权和 500 张目标均不会因创建这个桌面而自动开启。

## 已完成的离线校验

Python 编译检查通过；非 Linux/无交互终端被明确拒绝。Docker Compose CLI 在不访问 daemon、不读取真实环境配置的条件下，实际解析了完整工作台叠加配置和独立桌面配置：非 root、read-only、loopback 端口、无特权/额外 capabilities、强制 seccomp、私有 profile 挂载和资源上限均通过。合并时发现并修正重复 `no-new-privileges` 条目，保留原基础配置继承。

本离线交付尚未验证：基础镜像实际拉取、依赖安装、镜像构建、Linux 桌面、真实 Chromium 沙箱、现网兼容、网站访问与登录、图片入库。本次离线修改没有发起云端构建或后台临时服务；后续部署进度应另记实际验收回执。

五站入口补充已通过 27 项本地离线测试：五站 URL 与沙箱参数、落地页超时留窗且不重试、profile 锁排他与释放、不同来源分目录、未知来源与非交互拒绝、desktop 参数传递、Compose loopback/单来源挂载，以及原沙箱检查。测试使用合成浏览器对象，没有联网或真实登录；不替代上面的 Linux 实际启动验收。
