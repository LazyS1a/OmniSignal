# 授权逆向实验室

## 它解决什么问题

Ghidra、JADX、Apktool、Frida 和 mitmproxy 负责分析目标；OmniSignal 不重复实现这些工具。实验室只负责在工具之前和之后建立一条可审计链路：

`目标授权 -> 样本登记 -> SHA-256 校验 -> 隔离分析 -> 发现记录 -> 连接器晋级审查`

任何一步缺失都会拒绝继续。实验室不连接生产数据库，不读取生产凭证，也不会自动对外发起网络请求。

授权有效期按 UTC 自然日判断，避免宿主机、容器和后续 CI 处于不同时区时得到不同结论。

## 目录

- `allowlist.yaml`：允许分析的目标、授权依据、范围、有效期和可用工具。
- `samples/`：离线样本；每个样本必须有 manifest。
- `manifests/`：样本来源、用途、SHA-256 和数据标记。
- `artifacts/`：工具输出，只允许保存审查后的非秘密产物。
- `audit/operations.jsonl`：允许与拒绝操作的追加式日志。
- `tools.lock.yaml`：从官方 GitHub Releases API 固定的版本与资产哈希。

## 当前最简单的验证

在项目目录执行（默认使用断网隔离容器）：

```powershell
.\scripts\reverse-lab.ps1
```

成功时返回 `status=allowed`；修改样本、删除允许清单或更换哈希后会返回 `status=rejected`，同时写入审计日志。该容器没有网络、没有生产数据库、没有生产凭证，根文件系统只读，且移除了 Linux capabilities。

可单独复核隔离条件：

```powershell
.\scripts\verify-reverse-lab-isolation.ps1
```

四项都必须为 `true`：无外网、非 root、根文件系统只读、无数据库敏感环境变量。

## 把新样本放进实验室

1. 从 `allowlist.example.yaml` 复制出本机私有的 `allowlist.yaml`，再登记目标和授权范围。运行文件默认被 Git 忽略。
2. 将样本放在 `samples/` 下，不接受绝对路径或 `..` 路径。
3. 计算 SHA-256，并创建对应 manifest。
4. 使用 `VerifySample` 验证；通过前不要打开分析工具。
5. 分析结果只能写到 `artifacts/`，不得保存 Token、Cookie、私钥或无关真实用户数据；实验室目录整体不会进入生产镜像构建上下文。
6. 若结果要进入私有连接器，必须创建 finding 和 approved review；范围超出授权会被拒绝。

练习样本即使全部技术检查通过，也应明确写 `decision: rejected` 和原因，防止“能反编译”被误当成“应该进入生产采集”。

## 工具安装策略

`tools.lock.yaml` 已锁定五个官方 GitHub 项目的当前候选版本。工具按目标安装到 `D:\ReverseLabTools\OmniSignal`，不进入生产 Docker 镜像。Ghidra 和 JADX 体积较大，未选择对应目标前不自动下载；Frida/mitmproxy 也必须先确定主机与目标平台，避免安装错误架构。

当前已安装并验证 JADX 1.5.6；其 Windows with-JRE 资产、安装收据和运行文件均位于 D 盘。Ghidra、Apktool、Frida、mitmproxy 仍保持锁定但未安装，等具体授权目标出现后按需选择。

选择目标后按单个工具安装，安装器只接受锁文件中的官方 GitHub Release URL，并在解压前核对 SHA-256：

```powershell
.\scripts\install-reverse-tool.ps1 -Tool jadx
```

已存在但收据与锁文件不一致的目录不会被自动覆盖。

对已登记的 DEX/APK 使用统一 JADX runner。它会先走样本门禁，再核对工具安装收据，以相对路径调用 JADX，并为输出文件生成哈希收据：

```powershell
.\scripts\run-jadx-lab.ps1 `
  -Manifest .\reverse_lab\manifests\jadx_hello_v1_5_6.yaml `
  -OutputName jadx_hello_v1_5_6
```

相同样本和工具版本可安全复用既有结果；目录存在但收据不匹配时拒绝覆盖。
