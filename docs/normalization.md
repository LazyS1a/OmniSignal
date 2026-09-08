# 确定性标准化与质量层

这层只负责把获准采集的原始记录整理成稳定、可追溯的统一数据。它不评价产品，不分析痛点，不生成优化建议，也不把重复候选当成搜索份额。

## 运行链路

`ingested_records` 最新态 → 校验原始归档 → 按来源 profile 映射字段 → 确定性清洗 → 质量状态 → 实体别名命中 → 上下文边 → exact/near 重复候选 → 不可变标准版本。

入口：

```powershell
.\scripts\run-normalization.ps1
```

默认配置是 `config/normalization.yaml`，原始归档根目录是 `data/raw`。如需使用另一份经过审查的配置或归档目录：

```powershell
.\scripts\run-normalization.ps1 `
  -Config 'config\normalization.yaml' `
  -ArchiveRoot 'data\raw'
```

## 为什么结果可以重放

- 标准版本身份由来源、来源记录 ID、输入 raw hash、来源 schema、权限、归档 SHA、标准化器版本和配置 hash 共同决定。
- 同一批有效输入会得到同一个 normalization run id；再次运行直接复用，不增加记录、质量事件或重复组。
- 来源内容或配置改变时会生成新标准版本，旧版本保留，方便审计。
- 时间、语言和 URL 只按明确定义转换；语言缺失记为 `und`，不使用模型猜测。

## 统一字段

- `canonical_url`：只接受无账号密码的 HTTP/HTTPS URL，移除 fragment 和默认端口。
- `title`、`text`：Unicode NFKC、稳定空白和换行、配置长度上限。
- `published_at`、`updated_at`：只接受带时区的时间并统一为 UTC。
- `language`：采用来源声明或 profile 默认值。
- `entity_ids`：只按版本化别名配置命中。
- `parent_source_record_id`：只连接同来源、同批输入里明确存在的父记录。
- `raw_archive_sha256`：回到原始响应证据的内容摘要。

## 质量状态

- `accepted`：没有发现质量问题。
- `warning`：仍可处理，但存在语言未知、短文本、截断、无效可选字段等问题。
- `quarantined`：缺正文、缺必填字段或缺原始归档血缘，不能当作正常标准数据使用。

常见质量代码包括 `missing_required_field`、`missing_text`、`short_text`、`field_truncated`、`invalid_url`、`invalid_timestamp`、`invalid_language`、`language_undetermined`、`missing_raw_archive` 和 `context_target_missing`。

## 原始归档血缘

新连接器归档会写入 `record_links`，其中只有来源记录 ID 与 raw hash，不增加敏感字段。标准记录保存归档 SHA，不保存机器绝对路径。

历史归档回填必须同时匹配 `source_id + source_record_id + raw_hash`。若多个完整响应都包含同一精确记录版本，确定性选择 SHA 字典序最小的一份；若哈希不一致则不关联。归档内容与文件名摘要不一致、解压后超过 50 MB 或格式损坏时，整次运行停止。

## 数据库升级边界

- 有 Alembic 版本表：正常升级到最新 revision。
- 全新空库：正常创建。
- 未登记 SQLite：只接受与已知 0002 旧结构完全一致的库；先用 SQLite backup API 在数据库同目录备份并做完整性检查，再登记版本和升级。
- 未登记 PostgreSQL 或未知 SQLite 结构：停止并要求人工迁移审查，禁止自动 stamp。

遇到迁移失败时，不要删除数据库或手工创建 `alembic_version`。保留原库与 `pre-0003` 备份，先核对报错和 schema 差异。

## 增加新来源

先完成来源登记与连接器准入，再在 `config/normalization.yaml` 增加一个精确 `source_id` profile。字段映射只能引用该连接器已经允许落库的 payload 字段；未知来源保持 `unconfigured`，核心不会按名字猜字段。

修改字段映射、别名或重复策略时同步提升 `config_version`，跑完整测试并保留配置 diff。配置内容本身也参与 hash，所以规则变化不会覆盖旧标准版本。
