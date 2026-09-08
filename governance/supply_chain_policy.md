# OmniSignal 版本锁定与供应链规则

## 锁定

- Python 固定为 3.12 小版本线；直接依赖在 `pyproject.toml` 使用精确版本，间接依赖由 `uv.lock` 固定。
- uv 自身要求 0.12.8；锁文件由这一版本生成。若 uv 升级，单独提交锁文件差异。
- PostgreSQL 使用 18.6 Alpine 3.23，基础镜像固定 digest；gosu 1.19 由固定 Go 1.26.6 builder 摘要从上游模块版本重编。
- API 基础镜像固定为 Astral 官方 Python 3.12/uv 0.12.8 Alpine 3.23 的 linux/amd64 digest。
- Playwright Python 包、浏览器版本和容器镜像必须成套升级；不单独滚动升级 Chromium。
- 逆向实验工具固定发布包版本和 SHA-256，独立于生产依赖。
- 本机开发和扫描缓存统一放在 `D:\CodexCache\OmniSignal`；先运行 `scripts/use-d-drive.ps1`，避免再次挤满系统盘。

## 安全检查

Gate 3 开始执行：

1. Gitleaks 检查工作树和提交历史中的凭证。
2. Trivy 扫描源码/锁文件的漏洞与许可证。
3. 构建应用镜像后再次用 Trivy 扫描，并输出 CycloneDX SBOM。
4. 最终生产镜像的 Critical/High 默认阻断；例外必须限具体规则和路径、说明运行证据并设置到期日。
5. 不把“扫描通过”解释为没有风险；连接器夹具、越界测试和故障演练仍需单独执行。

## 升级窗口

- Dependabot 每周检查并创建 PR，但不自动合并。
- 普通依赖按月合批升级；一次只升级一组相关组件。
- 暴露面上的 Critical 漏洞在确认后立即停用受影响能力，目标 72 小时内修复或完成隔离；无法修复则回滚/替换。
- PostgreSQL、Playwright、Prefect、dlt 和 Scrapy 的主/次版本升级必须先在固定夹具上跑完整契约与恢复测试。
- Debian/glibc 与 Alpine/musl 之间切换 PostgreSQL 镜像族时，必须逻辑备份、新集群初始化和恢复，禁止直接复用原数据目录。

## 回滚

- 每次发布保留上一版 `uv.lock`、数据库迁移版本、容器 digest 和配置 schema。
- 数据库迁移先备份并验证恢复；不可逆迁移必须提供前滚修复方案，不能假装可降级。
- 新依赖版本失败时回退锁文件和镜像 digest，不在生产环境现场修改包。

## 适配器边界

业务层只依赖内部协议：`ApiExtractor`、`CrawlerDriver`、`BrowserDriver`、`Orchestrator`、`Store`、`AdminUIClient`。第三方异常在适配器处转换为统一错误类型。这样可以替换 dlt、Scrapy、Playwright、Prefect、PostgreSQL 或 Streamlit，而不改 ConnectorSpec 和领域模型。

## 不伪造 SBOM

Gate 2 已从 `uv.lock` 生成源码 SBOM，但不能把它冒充镜像 SBOM。完整发布仍必须从构建产物和容器镜像自动生成，补齐浏览器与操作系统包；生成步骤和目标文件见 `sbom/README.md`。
