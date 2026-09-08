# 可选处理插件 SDK

OmniSignal 使用 [pytest-dev/pluggy](https://github.com/pytest-dev/pluggy) 1.6.0 复用成熟的 hookspec、hookimpl 和注册校验。Pluggy 本身会在 host 中加载 Python 代码，所以 OmniSignal 把它放在独立 plugin host 进程，而不是 API、采集器或标准化核心进程。

## 这层负责什么

- 为语言识别、Embedding 或通用字段抽取提供统一批处理 hook。
- 按 manifest 只投递批准的标准字段。
- 核验插件入口源码 SHA-256。
- 限制单批条数、运行时间和输出字节数。
- 严格校验协议版本、插件身份、输出 schema 版本、记录 ID 和输出字段。
- 插件禁用、缺失、崩溃、超时或输出越界时返回独立失败结果，不修改采集和标准化数据。

这层不评价产品，不生成建议，也不允许插件覆盖原标准记录。

## Manifest

参考 `examples/processors/deterministic_text_stats.yaml`。关键字段：

- `sdk_version`：SDK 契约版本。
- `plugin_id` / `plugin_version`：插件身份。
- `entrypoint`：项目内经审查的 `module:object`。
- `runner_sha256`：入口源码的精确摘要。
- `input_fields`：插件能看到的标准字段；权限、归档路径、raw hash 和数据库信息不在允许集合。
- `output_fields` / `output_types` / `output_schema_version`：允许输出、JSON 类型和版本。
- `accepted_quality_statuses`：默认跳过 quarantined 标准记录。
- `timeout_seconds` / `max_batch_records` / `max_output_bytes`：资源边界。
- `enabled` / `approved`：必须同时为 true 才执行。
- `secret_refs`：Gate 6 必须为空；处理插件不接收连接器凭证。
- `trust_level`：当前只接受 `reviewed_project_code`。

## 编写插件

```python
from omnisignal.processing import hookimpl


class MyPlugin:
    @hookimpl
    def process_batch(self, records, options):
        return [
            {
                "normalized_id": record["normalized_id"],
                "values": {"my_field": "deterministic value"},
                "quality_status": "accepted",
                "quality_codes": [],
            }
            for record in records
        ]


plugin = MyPlugin()
```

`records[*].fields` 只包含 manifest 的 `input_fields`。采集正文即使包含“忽略指令”等文本，也只是普通 data；SDK 协议没有 system prompt，也不会自动调用模型。

## 调用 SDK

```python
from pathlib import Path
from omnisignal.processing import load_plugin_manifest, run_plugin

manifest = load_plugin_manifest(Path("examples/processors/deterministic_text_stats.yaml"))
execution = await run_plugin(
    manifest,
    normalized_documents,
    workspace=Path("data/workers/processors"),
    session=session,
)
```

传入 SQLAlchemy `session` 时，运行汇总和逐记录输出会持久化。相同 manifest 和相同标准输入会得到相同逻辑 `execution_id` 与 `output_hash`，每次实际尝试则有不同的 `run_id`，所以超时、失败和后续恢复不会互相覆盖。`PluginExecution.status` 可能是 `succeeded`、`failed` 或 `disabled`；失败时 `outputs` 为空，且不得让失败回滚采集或标准化事务。

## 明确边界

独立进程提供故障、超时、环境变量和输入字段隔离，但同一操作系统账号下的 Python 子进程不是强安全沙箱。`network_access: false` 是当前准入要求，不应被误解成操作系统已经强制断网。

因此 Gate 6 只允许运行项目内逐文件审查且摘要固定的插件。任意第三方 wheel、未知 GitHub 仓库代码、需要秘密或需要网络的插件都保持未批准；将来若要开放，必须先进入只读文件系统、无网络、非 root、资源限额的独立容器，并对完整制品而非单个入口文件做签名或摘要校验。
