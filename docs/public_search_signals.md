# 无账号公开搜索信号

本连接器复用 GitHub 的 GeneralMills/pytrends 4.9.2 获取 Google Trends，并解析已现场验证的 Google Suggest 公开响应。使用 signals 可选依赖；按次运行，不需要付费 API 或用户账号。

## 安装与运行

在项目根目录 PowerShell 执行：

```powershell
. .\scripts\use-d-drive.ps1
.\.bootstrap-venv\Scripts\uv.exe sync --locked --extra signals --extra crawl --extra ui --group dev
.\scripts\run-public-search.ps1 -Sqlite 'data\search-trial.db'
```

首次试用建议用独立 SQLite。省略 Sqlite 时沿用现有数据库配置。该入口采集一次后退出；不自动安装后台调度。输出 completed 只表示批次提交完成，部分来源可能降级，详见检查点 warnings 和归档。

编辑 `examples/policies/public_search_signals.yaml`：keywords 为 1–5 个不同查询词；search_property 为 youtube 或 web；geo 是 Trends 地区；timeframe 默认近三个月。同一来源检查点绑定配置，修改查询配置后应使用另一独立试验库，避免不同查询历史混写。

## 数据含义

| metric_type | value | 适用解释 |
| --- | --- | --- |
| relative_interest | 0–100 | 同次 Trends 查询中的归一化相对兴趣，可比较同组词的变化 |
| suggestion_rank | 1–10 | 采集当时公开联想词的顺序；不能换算搜索次数 |

联想请求未确认地域过滤生效，因此 geo 留空。Trends 未结束周期保留 is_partial。comparison_group 包含关键词、地区、语言、时间设置和实际观测时间范围；不同组不能直接拼成统一尺度。重复采集以来源记录 ID 幂等更新；滚动窗口变化会形成新比较组，同日联想词保留当日最新值。归档保存经解析的字段和版本，不是网络原始报文。

这些信号不能给出 Chrome 全体用户搜索记录、全站搜索总次数或真实市场份额。Google Trends 的采样和归一化说明：[官方 FAQ](https://support.google.com/trends/answer/4365533?hl=en)。

## 运行与故障

单次最多 1000 条、进程超时 45 秒，不自动重试。建议手动每天采集一次。429 时先停止并至少等待一小时，持续限流时停用来源。部分来源失败时保留成功记录并写 warnings，全部失败时批次失败；失败不能当作热度为零。当前没有跨进程的全局请求限速或定时调度，避免同时启动多个实例。

pytrends 仓库已归档，现场可用不代表未来一直兼容。版本与间接依赖由 uv.lock 固定，worker 协议及契约测试负责发现漂移，替换采集器时保留数据口径。来源失效时历史数据仍在库中，但应结合最后成功时间判断新鲜度。

现有运维界面可以查看来源和运行状态，尚未提供搜索信号趋势图；原始 payload 和 data/raw/public_search_signals 下的归档包含指标。该批数据不应直接套用正文标准化流程。

代码来源：[pytrends](https://github.com/GeneralMills/pytrends)。组件按能力和实际可用性选择，许可证信息仍记录用于后续分发判断。2026-09-05 对更新后的 uv.lock（含开发依赖）完成 Trivy 扫描，HIGH/CRITICAL 为 0；使用本地漏洞库 UpdatedAt=2026-09-01，发布前应更新漏洞库复核。
