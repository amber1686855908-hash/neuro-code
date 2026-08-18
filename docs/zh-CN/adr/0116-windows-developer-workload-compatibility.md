# ADR 0116：Windows 开发者工作负载兼容性基线

- 状态：已接受；W5 工作负载兼容性已在生产 W3/W4 路由验证
- 日期：2026-08-16
- 范围：通过 W3 与 W4 路由运行的普通 Windows 开发者工作负载

## 决策

W5 兼容性基线是对当前生产 W3/W4 路由的证据性测量。运行
`32194952573`（head
`75c07cb952765d8ab26ce8d219a39be7bc4e3ed5`）已在 Windows Server 2025
验证矩阵；工作负载 probe 不修改 Windows 沙箱实现、token 模型、setup
authority、ACL、Firewall、私有 profile、Job ownership 或 ConPTY。

每个工作负载先作为 HOST 对照运行，再用相同的已解析可执行文件和等价
argv，通过生产 W3 非 PTY `WindowsNativeLocalProcessSandbox.spawn()` 路由，
以及经由 `LocalInteractiveTerminalManager` 的生产 W4 PTY
`spawn_terminal()` 路由运行。主 profile 为 `WORKSPACE`，使用 Online identity。
工具缺失记录为 `NOT_INSTALLED`；观测结果属于证据，不构成放宽安全边界的理由。

专用 Windows job 产生有界 JSON 和 JUnit artifact。JSON artifact 才是工具来源、
每个 HOST/W3/W4 单元格、分类和关联分析的权威记录，而不是人工推断摘要。
不会记录 credential、环境 secret、handle 或无界完整输出。

## 冻结的安全 contract

矩阵保持已认证的 W1-W4 contract：

| Contract | 当前值 |
| --- | --- |
| 读隔离 | `LIMITED` |
| 写隔离 | `STRONG` |
| 网络隔离 | `STRONG` |
| 后代生命周期 | `STRONG_DESCENDANT_OWNERSHIP` |
| 主 profile | `WORKSPACE` 已支持 |
| 只读 profile | 已支持 |
| Strict profile | 因无法提供 strong read isolation 而失败关闭 |

`TokenRestrictedSids` 继续使用生产定义的精确有序集合；现有 privilege、ACL、
Firewall、私有 HOME/TEMP、identity、Job、named-pipe 和 ConPTY 边界均不变。
不得为使某个工作负载通过而添加 SID、privilege、fallback 或扩大 authority。

启用的 W3/W4 单元格只有在诊断事实同时满足选定的 Online W2 identity、
`IsTokenRestricted=true`、生产 restricting-SID set、启用的
`SeChangeNotifyPrivilege` 以及 unexpected enabled privilege 数量为 0 时，才会
输出 `token_attestation=PASS`。

## 矩阵工作负载

数据驱动的验收模块只覆盖确定性的启动和本地文件系统操作：

- `CMD_BASIC` 以及独立的 `CMD_NUL_REDIRECT` 退出码判据；
- 已安装时的 Windows PowerShell 与 `pwsh`；
- venv Python version、`-I -S`、`-I`、normal 启动、child-Python subprocess，
  以及经过验证的 base interpreter version 与 `-I -S` 启动行；
- Git version、授权 workspace 内 disposable repository 的 discovery 与
  `status --porcelain=v1`；
- Node version/`-e` 执行和实际解析到的 npm launcher；
- 仅运行 curl `--version`；
- 使用文档化 `CreateFileW(L"NUL")` 与 `WriteFile` 的原生
  `NUL_DIRECT_WIN32` probe；
- 在最终 restricted child token 中动态加载 `bcrypt.dll` 并执行
  `BCryptGenRandom`。

该矩阵不包含 package 下载、公共网络依赖、全局 Git 配置、execution policy
修改或兼容性 workaround。

## 结果解释

结果分类区分 `PASS`、`NOT_INSTALLED`、process creation/access 错误、设备访问拒绝、
runtime/dependency 初始化、repository discovery、timeout、非零退出、输出不匹配和
`INCONCLUSIVE`。已接受 artifact 中所有已安装的 W3/W4 单元格都先达到
`SpawnReady`，并在观察工作负载结果前保留通过的 token attestation。

## W5 兼容性证据记录

运行 `32194952573` 在 Windows Server 2025 hosted `windows-latest`、`WORKSPACE`、
Online identity 上完成 20 行矩阵。HOST、W3 非 PTY 与 W4 PTY 的所有单元格均为
`PASS / 0`：

| 工作负载 / variant | HOST | W3 非 PTY | W4 PTY |
| --- | --- | --- | --- |
| `CMD_BASIC` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `CMD_NUL_REDIRECT` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `POWERSHELL_BASIC` / Windows PowerShell | PASS / 0 | PASS / 0 | PASS / 0 |
| `PWSH_BASIC` / pwsh | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_MINIMAL_NO_SITE` / `-I -S` | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_ISOLATED` / `-I` | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_NORMAL` / normal | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_CHILD_PROCESS` / subprocess | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_BASE_VERSION` / verified base interpreter | PASS / 0 | PASS / 0 | PASS / 0 |
| `PYTHON_BASE_MINIMAL_NO_SITE` / base `-I -S` | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_REPO_DISCOVERY` / disposable repo | PASS / 0 | PASS / 0 | PASS / 0 |
| `GIT_STATUS` / porcelain v1 | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_VERSION` / default | PASS / 0 | PASS / 0 | PASS / 0 |
| `NODE_EXEC` / `-e` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NPM_VERSION` / resolved `npm.cmd` | PASS / 0 | PASS / 0 | PASS / 0 |
| `CURL_VERSION` / `--version` | PASS / 0 | PASS / 0 | PASS / 0 |
| `NUL_DIRECT_WIN32` / `CreateFileW` + `WriteFile` | PASS / 0 | PASS / 0 | PASS / 0 |
| `BCRYPT_CNG_RUNTIME` / dynamic `bcrypt.dll` + `BCryptGenRandom` | PASS / 0 | PASS / 0 | PASS / 0 |

按 access mode 的 NUL 证据也全部通过：

| NUL access mode | HOST | W3 非 PTY | W4 PTY |
| --- | --- | --- | --- |
| `NUL_READ`（`GENERIC_READ`） | Create PASS / error 0；未尝试写入 | Create PASS / error 0；未尝试写入 | Create PASS / error 0；未尝试写入 |
| `NUL_WRITE`（`GENERIC_WRITE`） | Create PASS / error 0；`WriteFile` PASS | Create PASS / error 0；`WriteFile` PASS | Create PASS / error 0；`WriteFile` PASS |
| `NUL_READ_WRITE`（两者） | Create PASS / error 0；`WriteFile` PASS | Create PASS / error 0；`WriteFile` PASS | Create PASS / error 0；`WriteFile` PASS |

artifact 保留了 Windows PowerShell、PowerShell 7、Python 及其 child process、Git
repository、Node/npm、curl、NUL 读写模式和动态 BCrypt CNG 启动的有界 PASS 证据。
每次自然完成的 W3/W4 还记录
`job_active_processes_after_quiesce=0` 与 `relay_threads_after_join=0`。矩阵是工作负载
证据，不是放宽 token、SID、ACL、environment 或 lifecycle contract 的授权。

## 下一决策边界

当前测量矩阵没有工作负载兼容性 blocker。未来 developer tool 或 network workload
必须拥有独立的有界 fixture 和 artifact 行；不能从这些本地启动 probe 推断，也不能用来
放宽当前安全 contract。
