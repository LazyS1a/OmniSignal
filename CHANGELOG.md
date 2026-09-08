# Changelog

本项目采用 [Semantic Versioning](https://semver.org/)；未发布变化记录在 `Unreleased`。

## Unreleased

### Added

- 新增 GitHub Pages 只读公网 Demo，使用内嵌合成快照展示总览、来源、任务、趋势、检索采样与证据质量界面。

### Changed

- 重构 GitHub 项目主页与公开协作入口。
- 明确区分公网静态演示与本地完整服务入口。
- 修复 CI 可选依赖安装范围和 Docker 构建上下文。

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
