# SearXNG 检索结果连接器

## 它解决什么

该连接器复用官方 SearXNG，把不同搜索引擎的公开结果变成统一、可归档的快照。首版按 `query × engine` 分开请求，因此 `position=1` 表示该引擎在本次固定配置样本中的第一条，不是所谓“全网第一”。

它可以保存查询词、引擎、类别、位置、URL、标题、摘要、公开发布时间、语言、观察时间和完整性标记。它不提供平台真实搜索次数，也不把多个引擎的结果混成一个虚构的全网排名。

## 本机部署

项目使用 SearXNG 官方镜像 `2026.9.5-c7f3080aa`，不跟随 `latest`。服务独立于 OmniSignal 主 compose，只绑定 `127.0.0.1:8888`；配置显式开启 JSON API，不对局域网或公网暴露。

Docker Desktop 启动并确认数据盘仍位于 D 盘后执行：

```powershell
.\scripts\start-searxng.ps1 -Pull
```

脚本会生成只存在于当前启动过程和容器环境中的随机 secret，等待健康检查，并验证 JSON API。它只启动搜索聚合服务，不运行 OmniSignal 采集任务。

查看状态，或同时读取最近 100 行日志：

```powershell
.\scripts\status-searxng.ps1
.\scripts\status-searxng.ps1 -LogLines 100
```

请使用包装脚本执行状态、日志与停止操作。Compose 配置要求创建容器时必须提供 secret；包装脚本会提供只用于解析配置的占位值，不会把运行中容器的随机 secret 写进项目。

停止服务：

```powershell
.\scripts\stop-searxng.ps1
```

## 单次采集

先在独立 SQLite 中试验：

```powershell
.\scripts\run-searxng-results.ps1 -Sqlite 'data\searxng-trial.db'
```

省略 `-Sqlite` 才会沿用项目数据库。统一总控台的“采集任务”页也会出现 `SearXNG 多引擎检索样本（示例词）`，只有人工点击并确认才运行；当前定时调度三层门禁仍全部关闭。

## 配置与稳定性

总控台示例现在使用 `examples/policies/searxng_dual_engine.yaml`，同时采样 DuckDuckGo 和 Brave；两个示例词、每引擎每词最多 10 条，共最多 40 条。命令行默认仍保留单引擎策略；双引擎命令行使用 `-Policy examples/policies/searxng_dual_engine.yaml`。2026-09-07 独立试验库单次取得四个完整切片、40 条 accepted 结果；这证明现场可用，长期稳定性仍需持续观测。

总控台“检索采样 → Web / SearXNG”已支持读取主库快照。每个关键词与引擎单独展示位置、证据与分母。需要实体统计时，在 `config/entity_sets.yaml` 登记固定版本的实体及别名/域名，再给 `config/collection_tasks.yaml` 的 SearXNG 任务设置 `entity_set: {id: 实体集ID, version: 1}`，重启总控台后供新任务使用。历史空实体集不会被重新解释成产品数据。

统计匹配标题和摘要中的别名，以及精确域名或其子域名；一条结果可匹配多个实体，比例之和可能超过 100%。比例仅覆盖该次引擎切片。

- 策略：`examples/policies/searxng_results.yaml`
- 连接器契约：`examples/connectors/searxng_results.yaml`
- 来源登记：`governance/source_registry.yaml`
- 容器配置：`compose.searxng.yaml` 与 `deploy/searxng/settings.yml`

策略只接受固定 `http://127.0.0.1:8888/search`，最多 20 个查询、8 个引擎、3 页，并限制总请求矩阵、单切片结果数、响应大小和运行时间。更换端口或实例必须同时修改 policy、connector spec 和 source registry，避免网页把任意 URL 传给 worker。

429、拒绝访问、超时、非法 JSON、字段漂移、空结果和未响应引擎都有固定状态。部分切片成功时保留成功证据并标记 `sample_complete=false`；全部失败时不写空值冒充零结果。当前 Docker 出站实测约需 5--6 秒，因此 SearXNG 内部上游请求时限固定为 12 秒、最大 15 秒，worker 外层为 20 秒；预算由外向内递增，但不能为了“稳定”设成无限等待。worker 只把明确的 `engines` 发给 SearXNG，`categories` 仅作本地记录标签，避免 SearXNG 把整个分类合并进请求并破坏引擎内排名。升级镜像前先在临时库和响应夹具上回归，失败则保留旧 tag。

本机已于 2026-09-06 成功拉起锁定镜像并通过容器健康与 JSON schema 探针；真实结果与归档是否通过，以 `progress.md` 的 S6.5 最终验收记录为准，不能只看容器 healthy。
