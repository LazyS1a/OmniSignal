# 部署、升级、回退与卸载

## 首次部署

生产式本地部署使用带版本的 Git tag，不直接依赖任意时间点的 `main`。数据库密码只放当前终端环境，API 和数据库默认仅绑定本机网络。

```powershell
git checkout v0.2.0
$env:OMNISIGNAL_DB_PASSWORD = "replace-with-a-strong-local-password"
docker compose up --build --detach
.\scripts\check-health.ps1
```

Compose 分开保存 PostgreSQL 数据和应用数据：`data/postgres` 保存数据库，`data/app` 保存原始归档与用户创建的不可变检索配置。两者都不进入 Git。

部署前可执行 `.\scripts\test-clean-environment.ps1`。它使用随机 Compose project、空数据库目录和独立回环端口，验证镜像构建、自动迁移、数据库断开时安全失败、恢复、API 重启、50 次有界只读冒烟、零采集和调度关闭；结束后精确清理本轮容器与数据，脱敏报告留在 `artifacts/private/clean-environment/`。这 50 次请求不是容量压测或 SLA。

## 升级

1. 停止提交新采集，确认没有在途任务；停用来源只阻止新任务，不会强杀 worker。
2. 执行 PostgreSQL 独立恢复演练，并额外备份 `data/app`；记录当前 tag、数据库 revision 和备份摘要。
3. 获取目标 tag，在干净环境跑测试，再重建并启动服务。容器入口会先执行 `alembic upgrade head`，失败时 API 不会启动。
4. 检查 `/health/ready`、来源、任务、审计和调度门禁；不要用一次 green readiness 代替来源新鲜度或长期稳定性。

## 回退

- 如果迁移尚未发生，直接回到旧 tag 并重建镜像。
- 如果新版本已经迁移数据库，先停写；不要在仍写入的库上盲目执行 downgrade。把升级前备份恢复到**新的独立实例/路径**并验证，再由操作者切换连接配置。
- 原库和失败版本都保留到核验完成，禁止把两个可写数据库合并或用恢复文件直接覆盖运行库。
- 依赖回退使用旧 tag 中固定的 `uv.lock`、Dockerfile 镜像 digest 和迁移代码，不单独手改某个包版本。

## 卸载

`docker compose down` 只停止并移除服务容器和网络，默认保留 D 盘数据目录。确认备份、保留期限和权限后，再由操作者明确选择是否归档或删除 `data/postgres`、`data/app`、`backups` 和私有运行报告；项目不提供“一键删库”命令。

## 仍需真实环境证明的事项

- 多日或多周连续运行、外部告警送达率和生产容量上限。
- 由实际备份频率决定的 RPO，以及跨机器/异地恢复的 RTO。
- 上游账号凭据轮换、配额和平台规则变化。

这些不阻塞本地稳定版发布，但不能写成已经具备的企业 SLA。
