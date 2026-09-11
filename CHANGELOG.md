# Changelog

本项目采用 [Semantic Versioning](https://semver.org/)；未发布变化记录在 `Unreleased`。

## Unreleased

### Added

- 新增 Docker 版 Windows 双击启动/关闭入口，一键启动 PostgreSQL、FastAPI 与 Streamlit 总控台。
- Docker 总控台首次启动生成本机私有 secrets 和独立数据目录，支持健康等待、幂等复用与端口冲突前置检查。

### Changed

- 应用镜像加入固定版本 Streamlit 运行依赖；Compose 新增只绑定本机回环地址的 UI 服务。

### Boundaries

- Docker 总控台不会自动启用调度或发起采集；SearXNG 仍为独立可选依赖。
- 关闭入口保留 PostgreSQL 与应用数据，不删除卷或私有 secrets。

## 0.2.0 — 2026-09-09

### Added

- 新增 GitHub Pages 只读公网 Demo，使用内嵌合成快照展示总览、来源、任务、趋势、检索采样与证据质量界面。
- 新增 PostgreSQL 独立容器/独立卷恢复与重启演练，输出脱敏摘要和本机实测耗时。
- 新增干净 Compose 环境验收，覆盖空库迁移、数据库中断安全失败与恢复、API 重启、零采集和调度关闭。
- 新增部署、升级、回退、卸载和连接器生命周期说明。
- 新增容器运行配置防回归测试；稳定版全量基线为 314 项测试。

### Changed

- 重构 GitHub 项目主页与公开协作入口。
- 明确区分公网静态演示与本地完整服务入口。
- 修复 CI 可选依赖安装范围和 Docker 构建上下文。
- 修复 API 镜像遗漏运行时测量/调度配置与 SearXNG 策略的问题。
- 为容器化 API 增加独立应用数据挂载，使原始归档和用户检索配置可持久化。
- PostgreSQL 备份脚本改为按 Compose 标签定位容器，不再让只读运维查询依赖当前终端中的数据库密码。

### Boundaries

- 定时采集仍默认关闭，干净环境验收不会请求外部平台。
- 本机恢复耗时不是生产 RTO；没有真实备份周期时不宣称生产 RPO。

## 0.1.0 — 2026-09-08

### Added

- 统一连接器契约、来源登记与版本化采集策略。
- 持久化任务、检查点、幂等写入和故障恢复链路。
- 原始证据归档、SHA-256 血缘与确定性标准化。
- FastAPI 运维接口与 Streamlit 黑蓝总控台。
- 公开搜索信号、YouTube、SearXNG、GitHub、静态网页等受控连接器。
- SQLite / PostgreSQL 支持、Alembic 迁移、Docker Compose 与恢复脚本。
- 312 项自动化测试基线和公开 CI。

### Boundaries

- 默认不开启周期采集。
- 不输出无法验证的绝对搜索量或全网份额。
- 不将单机验证表述为生产环境 SLA。
