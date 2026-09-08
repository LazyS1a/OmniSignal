# YouTube 视频检索采样占比

输入一个关键词和自家/竞品规则，采集 yt-dlp 视频搜索返回的前 K 个条目，输出各品牌出现次数、有效样本分母、采样比例、首次位置与逐条链接证据。现有运维台“数据与证据 → 检索采样”可直接查看快照。

## 配置可以稍后填写

用户的真实产品资料暂时留空，继续保留 YAML + --policy 入口。无需改代码：之后复制示例策略，填品牌/关键词/官方频道ID、设置 is_example=false，再通过 -Policy 指定文件即可。当前示例规则继续明确标记为示例，不会自动转成真实产品配置。

## 运维页面

启动现有 API 和 Streamlit 运维台，在“数据与证据”中选择“检索采样”。页面按搜索词筛选，按入库时间倒序，每页20个快照；选择后展示计数、分母、采样占比、首次位置、身份分类与逐条证据链接。采集超过24小时显示历史提示，来源最近一次失败也提示；这些都不代表后台开启了持续监测。

页面只读取当前 API 连接的数据库。此前示例在独立 data/youtube-visibility-trial.db，不会自动复制进主库；如果当前主库未采集过，页面正确显示空状态。要查看该示例，需让本地 API 连接该试验库；该试验库由采集器创建业务表，不含 Alembic 版本记录，因此不能据此宣称完整 readiness 已通过。正常部署沿用已有迁移流程。

只读接口：GET /ops/visibility?limit=20&offset=0&q=关键词，GET /ops/visibility/{snapshot_id}。列表上限50，详情最多50条视频、10个品牌。已删除快照隐藏；统计与证据不一致时返回异常标记/409，API 不透传任意原始字段。外部链接由已验证视频ID生成。API不可用时页面清除旧表格，不用演示值填充。

## 运行

在项目根目录 PowerShell 执行：

```powershell
. .\scripts\use-d-drive.ps1
.\.bootstrap-venv\Scripts\uv.exe sync --locked --extra signals --extra crawl --extra ui --group dev
.\scripts\run-youtube-visibility.ps1 -Output 'data\reports\visibility-example.json'
```

默认使用独立 data/youtube-visibility-trial.db，运行一次退出；不会安装定时任务。可用 -Policy 指定其他策略文件、-Sqlite 指定其他本地库。输出 JSON 报告；指定的输出文件已存在时跳过导出，数据库中的本次快照仍保留。records_seen=1 表示一个完整快照，视频条数看 report.valid_result_count。

策略在 examples/policies/youtube_visibility.yaml。默认 Notion/Obsidian 仅用于演示，is_example=true；不代表用户公司的产品。真实使用时填写 query、top_k（1–50）、language、自家品牌（恰好一个 owned）、竞品（competitor），并将 is_example 改为 false。

每个品牌配置稳定 id、name、aliases，以及可选 official_channel_ids（UC 开头的完整频道 ID）。频道名称或 @handle 不能用于官方身份认定。第一版只支持 YouTube 频道 ID，不接官网域名识别；官网信息可用于人工核对官方频道。

## 统计口径

- 品牌命中：标题含配置别名，或者上传频道 ID 精确属于配置的官方频道。官方频道的内容归属品牌，不保证该视频讨论某一个具体产品；品牌多产品共用频道时应慎用此规则。
- 标题匹配采用 Unicode NFKC、大小写归一化；英文名字检查词边界，例如 Notion 不命中 notional；中文使用文字包含。通用词仍可能误匹配，逐条 evidence 用于人工核验。多义词需要明确别名配置。
- 同一视频按 ID 去重，一个品牌在同一条结果重复出现只算一次，多品牌共现分别计数。因此品牌占比之和可超过 100%。
- sample_share_percent = 品牌命中条数 / 有效唯一视频条数 × 100，保留四位小数。仅取得完整 K 条且没有采集警告时给比例。
- 短批次、空结果、重复、解析异常或采集警告均标记 incomplete，保留已有计数但百分比为 null；不能当成零。完成批次中未命中才可显示 0%。
- first_position 是视频提取顺序中最早匹配位置。标题提及和官方频道结果均可计入。官方/第三方/身份未核实分列，官方频道 ID 没配置时不冒认第三方。

## 覆盖与限制

复用 [yt-dlp 官方项目](https://github.com/yt-dlp/yt-dlp) 的 ytsearch + flat extraction，仅取视频搜索元数据，不下载媒体。已核对锁定版本的 YoutubeSearchIE 使用 Videos only 筛选。采样不涵盖完整搜索页的广告、频道卡和其他布局，也不能代表每个真实用户看到的顺序。标题未提及但画面/语音/字幕讨论品牌的情况不计入。

请求 language 并保留 requested_language；实际生效地区未知，effective_geo=null，使用当前匿名采集网络环境，不能标成固定国家或设备结果。排名会随时间、网络和平台变化；跨关键词/跨平台不直接汇总成全网份额。

单次 worker 最多 60 秒、50 条，网络超时 10 秒、内部重试关闭。失败后批次不会产生零比例报告。建议每天手动一次；当前没有跨进程全局限流、自动调度或长期稳定性保证，避免并行启动。上游改版可更换 worker，匹配/统计/存储层无需跟随改写。

## 存储与复查

每次采样作为一条 visibility_snapshot 写入已有 IngestedRecord，包含采集时间、规则哈希、采集器/计算版本及证据。相同快照重放幂等；新的采集时间形成新快照，可比较历史。解析归档位于 data/raw/youtube_visibility，保留哈希回链，不宣称是完整网络报文。导出 JSON 是独立副本，需要用户自行管理其保留期限。

本次现场示例（2026-09-06 04:21:59 UTC）：关键词 AI note taking tools，10 条视频，无警告；示例 Notion 标题命中 1 条，比例 10%，首次位置 1；示例 Obsidian 为 0。两者均未配置官方频道，因此不输出官方身份结论。证据报告位于 data/reports/youtube-visibility-s2-example.json。这个结果只描述本次样本。
