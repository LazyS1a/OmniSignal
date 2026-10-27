# 首周每日分析试运行

这项流程在 Omni 服务器进程内运行，不需要本机 Codex 在线。它只为已有待处理图片创建分析任务；素材获取、权限确认、人工复核和知识发布继续使用各自的现有流程。

## 启用范围

默认关闭。由管理员在服务器配置 `OMNISIGNAL_WORKBENCH_DAILY_PILOT_CONFIG`，指向一个 UTF-8 JSON 文件。首周配置如下：

```json
{
  "start_date": "2026-10-27",
  "end_date": "2026-11-02",
  "timezone": "Asia/Shanghai",
  "weekdays": [0, 1, 2, 3, 4],
  "hour": 16,
  "minute": 0,
  "target": 30,
  "review_target_min": 10,
  "review_target_max": 20
}
```

字段必须完整且不能增加其它字段。日期、时区、工作日、16:00 和人工审核目标 10–20 均按本次首周试点固定；`target` 只接受 1–100 的整数，本次部署使用 30。配置不接受模型、凭据、权限或发布选项。人工批量分析的 `unlimited` 配置保持独立，首周结束也不会自动把每日目标提升至 100。

日期为北京时间，含 10 月 27 日与 11 月 2 日：共五个工作日。每个工作日 16:00 后的第一次有效检查可以创建当天唯一的一批；服务在当天晚些时候恢复，仍可执行当天的一批。周末不建批次，错过的往日不补跑，11 月 3 日起仅保留已有任务的只读报告刷新。

应用集成只能在 `create_app(start_worker=True)` 已获取的工作进程 OS 锁内启动 `DailyPilot.start()`；关闭时先 `DailyPilot.stop()`，再停止分析 worker，最后释放锁。调度线程每 30 秒检查一次，另用调度目录的 OS 锁避免重入。`snapshot()` 只返回内存中的安全状态，不调用 `tick()`、模型或入队方法；状态 API 的 GET 不会触发任务。关闭 worker 或移除配置后，自动调度停止，不自动取消已入队的任务。

## 每日执行过程

1. 严格校验配置和此前所有调度记录。任一记录损坏、时间倒退、历史批次无法核对时，停止新增任务，返回固定诊断码。
2. 先核对此前持久化的任务意图。批次已存在时，只读取批次状态；同一天尚未确认提交时，只使用原 request ID、原 items、原 actor 幂等恢复。已过期且数据库没有批次的意图记为 `expired_intent`，不提交旧批次。
3. 读取 `pending` 素材快照，按 `project_id`、否则 `source_url`、否则素材 ID 分组轮询，让多个项目优先获得覆盖机会。单轮最多检查 10,000 个候选，达到此安全扫描边界会在报告标明；不会偷偷增加当日目标。
4. 调用既有 `AnalysisService.preview_batch()` 检查当前修订、分析权限、已有分析、失败任务、额度和队列容量。未知或拒绝权限、已分析、已运行、失败需明确重试的素材都跳过，不修改素材权限。
5. 选满 30 张或用完候选后，把固定 UUIDv5 request ID、固定 items、`system:daily-pilot` actor、配置快照与校验值原子落盘，再调用既有 `enqueue_batch()`。提交会再次核验业务条件，因此竞争、修订或权限变化可令实际入队少于预检。当天不足时按实数记录，不稍后补第二批；零符合条件也会留下当天记录。
6. 由现有分析 worker 执行任务。调度器不调用模型、不读取或复制模型密钥、不自动审核或发布；失败只报告，不自动重试。

SQLite 批次表对 request ID、actor、payload 绑定。响应丢失后先查原批次；即使进程恰好在入队后崩溃，下一次仍能恢复同一批次，避免重复扣费。已经确认过的批次若消失、或者存在批次但丢失本地意图文件，自动调度会暂停，不重新猜测候选。

## 日报位置与字段

文件位于既有工作台数据目录下的 `operations/daily-pilot/`：

- `intent-YYYY-MM-DD.json`：持久化的调度意图与阶段。必须保留，不能通过删除它来重跑。
- `report-YYYY-MM-DD.json`：从批次状态刷新得到的日报，不包含密钥、模型原始响应或配置文件路径。

日报区分 `planned_images`（目标）、`eligible_at_plan`（预检选中）、`submitted`（实际入队）、`analysed_images`（模型分析完成）、`failed`、`queued` 和 `running`。`projects_covered` 是完成分析所覆盖的不同项目数，`submitted_projects` 是已入队覆盖的项目数，均不把图片数量当项目数量。

`skip_reasons` 汇总预检和入队复查的跳过原因；`submission_shortfall`、`analysis_shortfall` 分别表达入队缺口与分析完成缺口。队列未完成时，后者仍可能继续下降。

`requires_human_review` 仅统计该批已完成建议中仍在待处理区、当前修订与分析建议修订一致、且尚未绑定对应人工复核记录的素材。后续人工修订、排除或收录后，此数会变化；它不证明已完成多少次人工审核。`review_target_min/max` 是人工审核目标 10–20，`reviewed_count` 保持 `null`，不拿模型成功数冒充人工完成数。

安全状态包含 `enabled`、`state`、固定 `diagnostic`、安全 `policy`、北京时间 `current_day`、当天 `report` 和最近报告 `recent`。界面尚未有日报时应显示“尚无记录”，不能显示成已成功 0 张。状态为 `ended` 时仍可查看首周既有日报。

## 异常处理

`invalid_policy`、`invalid_state`、`batch_lookup_failed`、`observed_batch_missing`、`submission_uncertain`、`batch_without_intent` 等诊断表示需要管理员核查。状态不返回异常原文、网络响应、密钥或文件路径。当日意图已生成后修改配置会得到 `policy_changed_after_intent`，不会把同一天的 30 张扩成第二批。

保留原意图和批次数据再检查配置、SQLite 与日志。不要删除意图、伪造权限或修改稳定 request ID 来解除暂停；真正要重新分析失败素材时，仍由人工在现有详情页明确确认重试。

## 验收

定向测试：`python -m pytest tests/test_workbench_daily_pilot.py -q`。使用真实 SQLite Store/AnalysisService、合成图片和进程内假模型，不访问网络、不产生模型费用。覆盖每日边界、权限及失败跳过、跨天与崩溃恢复、项目覆盖、坏配置/坏状态、只读快照和人工分析不受试点目标限制。
