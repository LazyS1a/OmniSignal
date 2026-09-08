# 授权 Hook / 私有接口连接器

这条链路只解决“把已经获准、已经验证的 worker 输出稳定接入 OmniSignal”，不负责寻找目标、绕过限制，也不负责评价或改进产品。

## 一条数据怎么进来

1. 在逆向实验室中，使用已锁定的 Frida、mitmproxy、JADX 等 GitHub 开源工具分析自有、开源或明确授权目标。
2. 若确实需要私有接口或 Hook，把目标、授权范围、有效期、字段、请求预算和评审记录写入 `governance/source_registry.yaml`。
3. 把目标专用逻辑放进独立 worker。核心进程只通过版本化 JSON 作业/结果文件与它通信。
4. 核心在启动 worker 前核对入口、源码 SHA-256、授权有效期、目标、字段、速率和 schema 指纹。
5. worker 输出通过敏感字段、大小、条数、目标、协议和 schema 检查后，才会转换为统一 `RecordEnvelope` 并做幂等写入、checkpoint 与脱敏归档。

Frida 或 mitmproxy 是实验和适配工具，不是核心服务依赖。某个平台的 Hook 脚本、证书、补丁、Cookie 或测试账号不能放进公开仓库或核心进程。

## 本地模拟验证

项目自建 worker 不访问网络，也不使用账号。连续运行两次完成两页数据，再运行两次会只得到 unchanged：

```powershell
.\scripts\run-authorized-hook-connector.ps1
```

故障注入测试会主动模拟 401、403、schema 漂移、错目标、敏感字段、进程崩溃和超时：

```powershell
.\scripts\use-d-drive.ps1
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
.\.venv\Scripts\python.exe -m pytest -q tests\test_authorized_hook_connector.py
```

连续崩溃达到阈值后，`data/workers/<source_id>/circuit.json` 会保持熔断。确认授权、worker 版本和上游已经恢复后，操作员才能复位并执行：

```powershell
.\scripts\run-authorized-hook-connector.ps1 -ResetCircuit
```

## 接入真实已授权 worker

- 复制 connector manifest 与 policy，使用新的唯一 source id 和精确目标；默认保持 high risk、process/container、concurrency=1、max_attempts=1。
- worker 入口必须是项目 `src` 内的 `module:main`，接受 `--job` 与 `--result`，只在 result 路径原子写入 JSON。
- 按 `authorized_hook_protocol.py` 输出协议；不得把凭证、Cookie、请求头或未登记字段写入结果、日志、错误消息和归档。
- 在 registry 登记源码 SHA-256、输出 schema 指纹、授权到期时间、评审引用和 `env:VARIABLE_NAME` 形式的凭证引用。凭证值只放进运行环境，不能写进 YAML。
- worker 代码或 schema 每次变化都必须重新评审并更新哈希；旧 checkpoint 会因许可或指纹不同而拒绝复用。

当前的 process 模式提供故障隔离和最小环境传递，但不是对恶意代码的强安全沙箱。真实 Hook worker 若会加载不可信样本、需要系统级工具或访问授权网络，应进一步放进独立容器/虚拟机，并用外部出站白名单和速率代理约束；没有这些条件时不升级为真实来源。
