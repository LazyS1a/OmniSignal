# Pinterest 浏览器采集：专用运行环境与人工登录

记录日期：2026-11-05。本文是准备方案，不是服务器已部署或来源已验收的凭据。

## 当前真实状态

- 仓库已有浏览器 provider、调度对接及离线测试；离线 fake browser 成功不等于 Pinterest 实站成功。
- 本机用于测试的 `D:/coding/OmniSignal/.venv` 实测未安装 Playwright。项目 `browser` 可选依赖中声明 `playwright==1.62.0`，还包含 Scrapy 和 scrapy-playwright；基础依赖不会自动安装它们。
- 当前服务器运行环境尚没有本次所需的 Playwright/Chromium。仓库 Dockerfile 使用 Alpine，只安装 `signals`、`ui` extras，也没有安装 Chromium。本次没有登录服务器重新核验，更没有安装或重建镜像。
- 不直接向现有 Alpine 镜像加一条浏览器安装命令。浏览器 worker 应单独准备与 Playwright 匹配的受支持运行环境，再验收；当前 Dockerfile 保留原状。
- 用户在应用内置浏览器登录 Pinterest，不会自动传给这个 worker。不能复制其 Cookie、storage_state、个人浏览器配置或旧探测 profile。

## 最小可执行路径

先在本机准备一个新的专用 profile。人工登录与自动采集分开执行，并且使用同一个目录锁，避免同时打开损坏会话。

以下命令是供用户在本机 PowerShell 自行执行的准备示例，本次没有代为执行安装或登录：

```powershell
Set-Location D:/coding/OmniSignal-ad-workbench
# 使用项目已有 uv/锁文件准备 browser extra；安装前确认要使用的项目环境。
uv sync --frozen --extra browser
uv run --extra browser python -m playwright install chromium

# 此目录必须是新建空目录，或本脚本原来创建的专用目录。
uv run --extra browser python scripts/pinterest_browser_session.py prepare --profile-dir D:/coding/.novart-tmp/omni-browser-profiles/pinterest-collector

# 仅由用户在本机交互终端执行；将打开一个可见的新专用浏览器。
uv run --extra browser python scripts/pinterest_browser_session.py login --profile-dir D:/coding/.novart-tmp/omni-browser-profiles/pinterest-collector
```

`prepare` 只建立归属标记，不启动浏览器；输出 `playwright_installed` 仅表示 Python 包可导入，不能证明 Chromium 已安装。

`login` 拒绝非交互终端。用户在浏览器中正常输入自己的账号、密码和验证码，终端只接收完成后的 Enter。脚本不读取 Cookie、账号字段、页面正文或登录是否成功，不导出会话，不保存凭据文本。关闭后返回 `login_verified=false`：它只证明人工会话结束，没有声称采集可用。

请不要将个人 Chrome/Edge 的 User Data、旧探测目录或应用内置浏览器目录填入 `--profile-dir`。脚本拒绝未标记的非空目录、目录链接以及指向其他目录的 `chromium` 子目录。浏览器自身保存的专用会话目录应作为私有运行数据，不提交 Git，不作为交付材料打包。

## 接入调度前还要验收什么

1. 在私有配置的 Pinterest 条目中填入这个专用目录与真实公开列表 URL。配置引用 `OMNISIGNAL_WORKBENCH_BROWSER_CONFIG`；具体 schema 以仓库配置指南和 `browser_jobs.py` 为准，不在示例里写账号或凭据。
2. 自动采集仍使用原有 robots 检查、请求预算和请求间隔。人工登录不会覆盖 robots 禁止、已有访问拒绝或验证码状态，也不意味着已有展示和传输授权。
3. 小样验收真实列表、Pin 详情、canonical 身份、图片 `currentSrc/srcset`、下载尺寸与 SHA256。先确认原始图片完整入待处理区，用途仍为 `unknown`。
4. 验收 DOM 与允许的实际资源路径。当前只默认允许已观察的静态路径；若动态数据依赖其他请求，先记录正常页面的真实路径再审核配置，不猜接口，不自动扩大域名范围。
5. 登录失效或出现验证，停在对应状态；由用户正常处理后明确恢复。脚本不自动解验证码，不自动重试验证，不通过代理切换或其他 transport 绕过拒绝。
6. 有实际下一页链接才保存续跑游标；没有真实分页链时保持 `pagination_unverified`，有限滚动不能证明每日持续 500 张。

登录与采集验证成功后，才讨论持续 worker：本机常驻需要本机开机；迁到服务器需要在那里新建专用会话与受支持的浏览器运行环境，不将本机现有登录资料搬过去。每日 500 张是采集目标，不是已验证能力，也不等于 500 张已审核或已发布到 DeepFlare。

## 本次代码复核的保守边界

- 主图仅接受详情区里与该页 OG 主图相匹配的实际 `currentSrc/srcset`。站点换了结构或两个字段不一致，会报告图片不可用，不能猜测 `/originals/` 地址替代。
- 请求 Pin 与页面最终 Pin、canonical 不同即停止。此前人工确认的单张样本不能转成跨 Pin 的通用豁免。
- `request_limit`、`time_limit` 应保留项目进度供下一轮续跑；登录、robots、验证和身份冲突需单独处理，不能混成同一种“重试”。
- 策略等待现在计入运行预算，超时后不会启动浏览器。不过该预算不是能中断所有阻塞调用的硬截止：单次导航当前最多等待 25 秒，robots 策略和限速等待也可能在返回后才检查超时（间隔上限 300 秒）。上线验收要测真实慢页面、静态资源拒绝和中途暂停，不能将 `max_seconds` 宣称为精确墙钟时限。

本次准备脚本未安装、未启动、未连接任何用户会话，也没有采集、分析、发布或修改线上来源配置。
