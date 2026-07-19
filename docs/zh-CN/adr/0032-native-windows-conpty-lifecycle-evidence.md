# ADR 0032：通过 ConPTY 验证 Windows 原生终端生命周期

**简体中文** · [English](../../en/adr/0032-native-windows-conpty-lifecycle-evidence.md)

- 状态：已接受
- 日期：2026-07-19
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

Textual 无头测试可以证明应用组合与关闭，却不会创建 Windows 控制台会话，也无法执行
虚拟终端输入、resize 和清理。Linux/macOS 标准库 PTY 冒烟测试已经提供 POSIX
进程边界证据，但不能证明生产 CLI 由 Windows ConPTY 托管时会恢复备用屏幕、光标、
focus tracking 以及宿主上任何可用的父控制台 mode。

ConPTY 使用同步输入与输出通道。Microsoft 明确要求关闭期间在独立线程持续排空输出，
因为 `ClosePseudoConsole` 可能发送最后一帧；旧版 Windows 在输出通道无人处理时还可能
无限阻塞。进程创建同时需要包含 `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 的
`STARTUPINFOEX` 属性列表。

## 决策

私有 `windows_conpty` 平台适配器只使用 Python 标准库。它惰性加载 Windows 10 1809
或更高版本的 `kernel32.dll` API，创建两条同步管道和伪控制台，再通过可变命令行、排序后
的 Unicode 环境块、`EXTENDED_STARTUPINFO_PRESENT` 与伪控制台进程属性启动托管进程。
项目不会因此增加 Windows 专用运行依赖。

托管进程的 `STARTUPINFO` 还会设置 `STARTF_USESTDHANDLES` 和空标准句柄占位符，避免 CI
runner 等拥有控制台的父进程在 ConPTY 建立托管控制台连接期间把自身标准句柄泄漏给
子进程。

适配器掌控伪控制台、宿主管道端、进程句柄和一个专用输出排空线程。输入写入会处理部分
进度；resize、等待、非零退出码、显式终止与关闭均为可观察操作。捕获结果只保留有界
首尾，但读取线程仍会排空全部字节。每个创建阶段都有清理测试。正常关闭会先停止仍存活
的托管进程、关闭宿主输入，在 `ClosePseudoConsole` 调用期间维持输出读取，随后关闭输出
与进程句柄。清理幂等，并且只在尝试释放全部剩余资源后报告第一个错误。

该适配器最初用于可执行文件边界验证，现在已经实现
[ADR 0034](0034-bounded-owned-interactive-terminal-sessions.md) 定义的共享交互式终端
平台端口 Windows 一侧；它仍未作为 ACP 方法或工具公开。可移植假 API 测试覆盖管道、
句柄与 Job 所有权、部分写入、resize、等待/终止、有界捕获、回调、所有主要失败阶段，
以及准确的伪控制台/Job `STARTUPINFOEX` 属性与 Unicode 环境契约。

选择性 `terminal-smoke` CI 矩阵现在包含 Windows。原生测试会：

- 在 ConPTY 中启动真实、离线的生产 CLI；
- 等待进入应用模式，resize 伪控制台，发送真实 `Ctrl+C`，验证空闲 TUI 继续存活，再发送
  真实 `Ctrl+Q`；
- 要求生产退出码为零，备用屏幕、光标和 focus tracking 的清理序列成对且顺序正确，在
  控制台句柄可用时父控制台 mode 保持不变，输出有界，并且夹具凭据不泄露；
- 运行另一个控制台探针，观察初始与 resize 后的尺寸，同时保留退出码 7。

POSIX 原生测试也同时得到加固：只有无头测试使用 Textual 自动按键 hook。Linux/macOS
现在会在观察到应用模式建立后，真正通过 PTY 写入 `Ctrl+Q` 字节。

## 影响

- 三类目标平台现在都在仓库中具有原生、进程边界终端生命周期覆盖，不再从无头驱动推断
  Windows 行为。
- ConPTY 输出在清理期间持续排空，避免一类已知旧版 Windows 死锁；即使子进程产生大量
  输出，所有保留内容仍有边界。
- ADR 0034 新增有类型 port、有界游标环形缓冲、权限、工作区、沙箱集成与应用所有权；
  Neuro Code 仍不宣称已经暴露 ACP 交互式 PTY 协议。
- 普通 Windows `ProcessTree` 现在通过
  [ADR 0033](0033-atomic-windows-job-process-creation.md) 的独立边界使用
  `PROC_THREAD_ATTRIBUTE_JOB_LIST`。生产 ConPTY 现在会在同一次创建调用中组合伪控制台
  与 Job 列表属性，同时仍与非 PTY Shell 流所有者保持分离。
- Windows 原生结果需要 Windows runner。Linux 开发环境会执行可移植 API/结构契约，并把
  原生用例报告为平台跳过。PR #6 的
  [CI 运行 29680149723](https://github.com/amber1686855908-hash/neuro-code/actions/runs/29680149723)
  已提供成功的 Windows 3.12/3.14 全量测试和原生 ConPTY 终端冒烟证据。

Win32 生命周期遵循 Microsoft 的
[伪控制台会话指南](https://learn.microsoft.com/zh-cn/windows/console/creating-a-pseudoconsole-session)
与
[ClosePseudoConsole 要求](https://learn.microsoft.com/zh-cn/windows/console/closepseudoconsole)。
历史行为证据来自只读固定基线中的
`crates/codegen/xai-grok-pager-pty-harness/src/pty.rs`、
`crates/codegen/ptyctl/src/pty.rs` 和
`crates/codegen/xai-grok-pager/tests/pty_e2e_minimal.rs`。
