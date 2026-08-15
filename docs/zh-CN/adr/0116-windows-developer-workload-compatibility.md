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

## 矩阵工作负载

数据驱动的验收模块只覆盖确定性的启动和本地文件系统操作：

- `CMD_BASIC` 以及独立的 `CMD_NUL_REDIRECT` 退出码判据；
- 已安装时的 Windows PowerShell 与 `pwsh`；
- Python version、`-I -S`、`-I` 和 normal 启动行；
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

## 下一决策边界

审阅 focused CI artifact 后，按受影响工作负载数量、W3/W4 是否共享、对开发工作流
的影响以及是否无需削弱冻结的安全 contract 来排序最高影响兼容性候选。Gate 0 不
实现该候选，W5 Gate 1 尚未开始。

