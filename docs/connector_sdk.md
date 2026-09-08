# ConnectorSpec 与连接器 SDK

## 先把它理解成“统一插头”

不同平台说话方式不同：REST API 可能按 cursor 翻页，网页需要 CSS 选择器，动态页面要浏览器，WebSocket 持续推送，Hook runner 又是独立进程。如果每种来源都直接往数据库里塞数据，项目很快会变成六套互不兼容的脚本。

OmniSignal 的做法是固定中间边界：

1. `ConnectorSpec` 说明这个来源允许访问哪里、采用什么鉴权引用、怎样分页、提取哪些字段、如何保存检查点、怎样同步删除以及需要怎样隔离。
2. 第三方框架负责“干活”。dlt、Scrapy、Playwright 或 Hook runner 都只能藏在适配器后面。
3. 所有适配器向核心返回相同的 `RecordEnvelope`、`Checkpoint`、`CollectBatch` 和 `DeleteBatch`。
4. 核心系统不需要知道记录来自 dlt 还是 Scrapy，只根据统一契约去校验、存储、审计和恢复。

## 一次采集的完整链路

`读取 ConnectorSpec → 本地校验 → 健康检查 → 读取旧 Checkpoint → 有界采集一批 → 输出 RecordEnvelope → 核心提交数据 → 核心提交新 Checkpoint`

检查点必须在数据成功提交后更新。反过来先写检查点，一旦数据库写入失败，就会跳过尚未保存的数据。

## 为什么返回批次而不是一次抓完

- 可以限制单次内存、时间和请求量。
- 运行中断后从上一个已提交检查点继续。
- 429、5xx 或浏览器崩溃只需重跑当前批次。
- 同一批次重放时 `raw_hash` 保持一致，存储层可以做幂等去重。

## 错误不是一律重试

| 错误 | 默认动作 | 原因 |
| --- | --- | --- |
| 401/认证失败 | pause | 继续请求只会制造更多失败 |
| 403/权限不足 | pause | 不自动换账号或扩大权限 |
| 429/限流 | retry | 遵守退避和请求预算 |
| 5xx/断网 | retry | 通常是暂时故障 |
| schema 漂移 | quarantine | 隔离异常数据，不能静默错列 |
| 越界/政策违规 | disable | 立即停用对应连接器 |
| Hook runner 崩溃 | retry 后熔断 | 只影响独立 runner |

## 安全边界

- Spec 只接受 `secret_ref`，不接受 Token、Cookie、密码等明文额外字段。
- 网络目标必须使用与连接器匹配的协议；URL 中禁止内嵌账号、密码或密钥查询参数。
- 文件来源只接受连接器工作区内的相对路径，拒绝盘符绝对路径、`file://` 和 `..` 越界。
- `field_allowlist` 明确拒绝凭证、私钥、证件和精确位置等禁止字段。
- Browser 与授权 Hook 不能在核心进程内运行。
- 授权 Hook 必须是 `high` 风险、独立进程/容器并启用 kill switch。
- 外部文本只存在 `payload` 中，不能修改 Spec 或错误处置逻辑。

## 兼容规则

- `spec_version` 和 `output_schema_version` 当前都是 `1.x`。
- 增加可选字段属于兼容修改；删除字段、改变含义或修改默认安全行为需要新主版本。
- 连接器自身使用独立 `connector_version`，升级连接器不等于升级核心协议。
- 核心代码只依赖 `Connector` Protocol，不导入 dlt、Scrapy、Playwright 或 Prefect 类型。
- 机器可读契约保存在 `schemas/connector-spec.schema.json`，测试会阻止运行时模型与该文件静默分叉。

## 当前模拟实现能证明什么

`FixtureConnector` 能证明配置解析、统一输出、错误分类、检查点续跑和确定性重放。它不能证明真实平台的授权、限流、页面结构、浏览器稳定性或 Hook 兼容性；这些必须在 Gate 4 的对应连接器中分别测试。
