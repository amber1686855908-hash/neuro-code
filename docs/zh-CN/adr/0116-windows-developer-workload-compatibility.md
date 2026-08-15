# ADR 0116：Windows 开发者工作负载兼容性基线

- 状态：W5 Gate 0 证据采集；不包含生产兼容性修复
- 日期：2026-08-16
- 范围：通过 W3 与 W4 路由运行普通 Windows 开发者工作负载

## 决策

W5 先建立只读兼容性基线。证据分支测量 W4 合入后的固定树
`716d56c2e769af5868e03d8e05d15eadec1cd8df`；不修改 Windows 沙箱实现、
token 模型、setup authority、ACL、Firewall、私有 profile、Job ownership
或 ConPTY。

每个工作负载先作为 HOST 对照运行，再使用相同的已解析可执行文件和等价
argv，通过生产 W3 非 PTY `WindowsNativeLocalProcessSandbox.spawn()` 路由，
以及经由 `LocalInteractiveTerminalManager` 的生产 W4 PTY
`spawn_terminal()` 路由运行。主 profile 为 `WORKSPACE`，初始使用 Online
identity。缺失工具记录为 `NOT_INSTALLED`；观察到的工作负载失败属于证据，
不得自动实现兼容性修复。

专用 Windows job 产生有界 JSON 和 JUnit artifact。JSON artifact 才是工具
来源、每个 HOST/W3/W4 单元格、分类及关联分析的权威记录，而不是人工推断的
摘要。不会记录 credential、环境 secret、handle 或无界完整输出。

## 冻结的安全 contract

矩阵必须保持已认证的 W1-W4 contract：

| Contract | 当前值 |
| --- | --- |
| 读隔离 | `LIMITED` |
| 写隔离 | `STRONG` |
| 网络隔离 | `STRONG` |
| 后代生命周期 | `STRONG_DESCENDANT_OWNERSHIP` |
| 主 profile | `WORKSPACE` 已支持 |
| 只读 profile | 已支持 |
| Strict profile | 因无法提供 strong read isolation 而失败关闭 |

`TokenRestrictedSids` 继续是安装级 write SID 的精确单例；
`SeChangeNotifyPrivilege` 以及现有 privilege、ACL、Firewall、私有
HOME/TEMP、identity、Job、named-pipe 与 ConPTY 边界均不变。不得为使某个
工作负载通过而添加 SID、privilege、fallback 或扩大 authority。

对于启用的 W3/W4 单元格，只有当诊断事实同时满足选定的 Online W2 identity、
`IsTokenRestricted=true`、精确单例 synthetic write SID、启用的
`SeChangeNotifyPrivilege` 以及 unexpected enabled privilege 数量为 0 时，
才会输出 `token_attestation=PASS`。

## 矩阵工作负载

数据驱动的验收模块只覆盖确定性的启动和本地文件系统操作：

- `CMD_BASIC` 以及独立的 `CMD_NUL_REDIRECT` 退出码判据；
- 已安装时的 Windows PowerShell 与 `pwsh`；
- venv Python version、`-I -S`、`-I` 和 normal 启动行，以及经过验证的 base
  interpreter version 与 `-I -S` 启动行；
- Git version、在授权 workspace 内 disposable repository 中的 repository
  discovery、`status --porcelain=v1`；
- Node version/`-e` 执行和实际解析到的 npm launcher；
- 仅运行 curl `--version`；
- 仅用于验收的原生 `NUL_DIRECT_WIN32` probe，使用文档化的
  `CreateFileW(L"NUL")` 与 `WriteFile`。

不包含 package 下载、公共网络依赖、全局 Git 配置、execution policy 修改或
兼容性 workaround。当前 W3 证据中没有精确的既有 restricted-curl 命令，因而
不会伪造一个替代命令作为复现。

## 结果解释

结果分类区分 `PASS`、`NOT_INSTALLED`、process creation/access 错误、设备访问
拒绝、runtime/dependency 初始化、repository discovery、timeout、非零退出、
输出不匹配和 `INCONCLUSIVE`。HOST 失败是 fixture/工具证据，不是沙箱兼容性证据。
HOST 通过而 W3、W4 均失败时，记录为共享 restricted-runtime 候选；仅 W3 或仅
W4 失败则保留为 transport-specific 证据。这些只是下一阶段的假设，不是修复或
因果结论。

## Gate 0 证据记录

PR body 是最新 exact-head CI run 与 artifact 的 canonical pointer。本 ADR
有意不硬编码易变化的当前 run ID；历史 run ID 只有在明确标注其测试的 exact
commit 时才保留。`windows-native-sandbox-compatibility` job 必须执行一个
矩阵测试、0 skip，并上传有界 JSON 与 JUnit artifact。矩阵运行于 Windows
Server 2025 hosted `windows-latest`、`WORKSPACE` 和 Online identity。

controller 记录的工具来源：

| 工具 | 已解析路径与版本 |
| --- | --- |
| `cmd.exe` | `C:\Windows\System32\cmd.exe`；Windows 10.0.26100.33158 |
| `powershell.exe` | `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`；5.1.26100.33158 |
| `pwsh.exe` | `C:\Program Files\PowerShell\7\pwsh.exe`；7.6.4 |
| `python.exe` | `D:\a\neuro-code\neuro-code\.venv\Scripts\python.exe`；3.12.10 |
| base `python.exe` | 从 venv 已验证的 `sys._base_executable` 发现；路径与版本记录在 JSON artifact |
| `git.exe` | `C:\Program Files\Git\bin\git.exe`；2.55.0.windows.3 |
| `node.exe` | `C:\Program Files\nodejs\node.exe`；v22.23.2 |
| `npm.cmd` | `C:\Program Files\nodejs\npm.cmd`；10.9.8 |
| `curl.exe` | `C:\Windows\System32\curl.exe`；8.16.0 Schannel |

下表记录 `HOST / W3 / W4` 的 `classification` 与退出码；`T` 表示达到有界
timeout。所有已安装的 W3/W4 单元格都先达到 `SpawnReady`，并在观察工作负载
结果前保留通过的 token attestation。

| 工作负载 / variant | HOST | W3 非 PTY | W4 PTY |
| --- | --- | --- | --- |
| `CMD_BASIC` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `CMD_NUL_REDIRECT` / default | PASS / 0 | DEVICE_ACCESS_DENIED / 1 | DEVICE_ACCESS_DENIED / 1 |
| `POWERSHELL_BASIC` / Windows PowerShell | PASS / 0 | RUNTIME_INITIALIZATION_FAILURE / 4294901760 | RUNTIME_INITIALIZATION_FAILURE / 4294901760 |
| `PWSH_BASIC` / pwsh | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_VERSION` / default | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_MINIMAL_NO_SITE` / `-I -S` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_ISOLATED` / `-I` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `PYTHON_NORMAL` / normal | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `GIT_VERSION` / default | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `GIT_REPO_DISCOVERY` / disposable repo | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `GIT_STATUS` / porcelain v1 | PASS / 0 | DEVICE_ACCESS_DENIED / 128 | DEVICE_ACCESS_DENIED / 128 |
| `NODE_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_EXEC` / `-e` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NPM_VERSION` / resolved `npm.cmd` | PASS / 0 | PASS / 0 | PASS / 0 |
| `CURL_VERSION` / `--version` | PASS / 0 | TIMEOUT / 1 / T | TIMEOUT / T |
| `NUL_DIRECT_WIN32` / `CreateFileW` + `WriteFile` | PASS / 0 | DEVICE_ACCESS_DENIED / 2（Win32 5） | DEVICE_ACCESS_DENIED / 2（Win32 5） |

artifact 保留的重要有界错误事实：Windows PowerShell 在 CLR 启动时报告
`HRESULT 80070005`（Win32 `2147942405`）；Git 报告
`fatal: could not open '/dev/null' for reading and writing: Permission denied`；
直接 NUL probe 报告 `CreateFileW` error 5；`pwsh` PTY 输出包含有界的
`BCrypt.dll` 初始化失败（`0x8007045A`）。Python 的任何启动变体都没有产生
用户 marker。curl 行只测启动；当前 W3 evidence 没有精确的既有 restricted-curl
命令，因此没有运行替代网络命令。

解释至少分为两个证据 cluster。Cluster A 是 device/NUL 证据：
`CMD_NUL_REDIRECT`、按 access mode 分开的 `NUL_DIRECT_WIN32` probe，以及
Git 的 `/dev/null` 启动失败。Cluster B 是 runtime initialization：Windows
PowerShell 的 `HRESULT 0x80070005`、pwsh 的 `BCrypt.dll`/`0x8007045A` 输出、
Python 启动 timeout 和 curl 启动 timeout。Node 与 npm 在两种 transport 中
均通过。W3/W4 共享模式是事实，但 cluster 之间的关系仍未证明；没有新证据
时，不把 Python 等同于 BCrypt、不把 curl 等同于 BCrypt，也不把 PowerShell
等同于 NUL。

## 下一决策边界

下一阶段首先调查影响多个 W3/W4 工作负载的 restricted startup/device
compatibility seam；在任何改动之前仍必须分离其中的子原因。Gate 0 不授权
CNG/Bcrypt、token、SID、privilege、ACL、Firewall 或 fallback 变更，也不实现
修复，W5 Gate 1 尚未开始。兼容性 timeout 行记录 canonical termination、runner
状态和有界 drain 事实；当没有测量 `orphan_count` 时，不声称观察到
`orphan_count=0`。
