# Contributing to OmniSignal

感谢你愿意参与 OmniSignal。项目优先接受能提升可靠性、可解释性和可复现性的改动，而不是单纯增加来源数量。

## 开始之前

1. 先搜索现有 Issue，确认问题没有重复。
2. 新连接器必须说明数据来源、允许范围、失败语义与字段最小化策略。
3. 不要在 Issue、测试夹具、日志或提交中放入 Cookie、Token、账号、私有响应或个人数据。

## 本地开发

```powershell
git clone https://github.com/LazyS1a/OmniSignal.git
cd OmniSignal
uv sync --frozen --group dev --extra crawl --extra ui
uv run pytest -q
```

提交前至少执行：

```powershell
uv lock --check
uv run pytest -q
uv run python -m compileall -q src tests migrations
```

涉及容器时另执行：

```powershell
$env:OMNISIGNAL_DB_PASSWORD = "local-build-placeholder"
docker compose build
```

## Pull Request 要求

- 一次 PR 只解决一个明确问题。
- 描述行为变化、风险、验证命令与结果。
- 新功能必须补测试；修复故障应优先补回归测试。
- 保留 fail-closed、安全边界和来源证据，不通过吞错把失败伪装成零结果。
- 不提交 `data/`、`backups/`、运行日志、浏览器配置、凭据或真实私有采集样本。

提交 PR 即表示你同意贡献内容按项目的 MIT License 发布。
