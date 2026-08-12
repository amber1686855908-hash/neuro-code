# ADR 0034：有界且受控的交互式终端会话

**简体中文** · [English](../../en/adr/0034-bounded-owned-interactive-terminal-sessions.md)

- 状态：已接受
- 日期：2026-07-19
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

原生终端冒烟测试与 Windows ConPTY 所有者已经建立了有用的平台证据，但还没有提供 ACP
端点或其他界面可以安全消费的应用契约。直接暴露测试辅助类会绕过权限、工作区、沙箱、
输出内存、进程树和关闭边界。

终端契约明确覆盖命令、工作目录、环境、尺寸、有界读取、输入、resize、等待和受控进程关闭。

## 决策

定义与供应商、界面无关的终端领域值和端口：

- `TerminalSize` 校验正数且平台安全的尺寸；
- `TerminalSignal` 区分中断、终止和强制终止；
- `TerminalOutputChunk` 返回字节、单调递增的下一游标、该游标前被丢弃的字节数和 EOF
  标记；
- `InteractiveTerminalManager` 创建仅 exec 会话并持有关闭生命周期；
- `InteractiveTerminalSession` 暴露有界读取/写入、resize、信号、限时等待和幂等关闭；
- `TerminalPlatform` 是同步操作系统适配器边界，通过回调投递输出、EOF 和失败。

`LocalInteractiveTerminalManager` 最多保留配置数量的活动或创建中会话。每个会话具有随机
不透明 ID 和线程安全、按游标寻址的尾部环形缓冲。环形缓冲上限为 16 MiB，单次读取或
写入上限为 1 MiB，单次阻塞读取请求上限为 60 秒。读取方落后时会收到准确的丢弃字节数，
不会把保留输出误认为完整 transcript。输出只存在于内存，不加入持久模型/会话历史。

创建顺序如下：

1. 校验 argv、尺寸、容量和环境；
2. 在配置工作区内解析一个已存在的目录；
3. 判定带副作用的 `create_terminal` 权限；结果为 `ask` 时取得异步审批；
4. 剥离配置的受保护环境变量，由应用替换终端/pager 控制值，并且只把不透明环境指纹放入
   审批 scope；
5. 启用沙箱 profile 时，必须取得匹配的 `LocalProcessSandbox` 并提交 typed
   `SandboxedProcessRequest`；
6. 通过所选 `TerminalPlatform` 启动。

不存在 Shell 字符串回退。审批拒绝、沙箱缺失或不匹配、工作区逃逸、不支持的平台或适配器
失败，都会在返回会话之前失败关闭。

POSIX 使用原生 PTY，让入口成为新 session/进程组所有者，并对整个进程组发送中断、终止和
强制终止。Windows 把现有 ConPTY 所有者投影到共享端口。生产 ConPTY 创建现在会建立
关闭即终止的 Job，并在同一次 `CreateProcessW` 属性列表中传入
`PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` 与 `PROC_THREAD_ATTRIBUTE_JOB_LIST`，因此托管
进程在进程树所有权生效前无法执行任何指令。ConPTY 输出持续排空，Job、进程、管道和
伪控制台只有一条明确关闭路径。中断使用虚拟终端 Ctrl+C 输入；终止与强制终止作用于整个
Job。

原生启动和平台操作都不占用事件循环线程。如果创建在操作系统调用开始后被取消，运行时会
等待该有界调用结束，并在传播取消前关闭任何已经返回的所有者。管理器关闭时先把注册表
标为关闭，等待创建中会话清理，再关闭全部已注册会话。不允许没有引用的即发即弃所有者。

## 影响

- ACP 与未来界面可以依赖同一个有界生命周期契约，无需导入 POSIX、Win32、Textual 或
  仅供测试的代码。
- 输出截断通过游标显式且可继续读取，但这不是持久化完整终端 transcript 服务。
- 受保护环境值不会到达子进程或审批界面。其他命令参数保持可见，因为用户必须能审查将要
  授权的进程。
- Linux 测试执行真实 PTY 输入、resize、信号和非零退出；可移植 fake/ctypes 契约覆盖
  Windows 回调、Job 所有权、属性与清理。PR #6 的
  [CI 运行 29680149723](https://github.com/amber1686855908-hash/neuro-code/actions/runs/29680149723)
  已提供成功的 Windows 原生全量测试与 ConPTY 冒烟证据。
- 本切片有意不发布 ACP 方法。ACP stdio/WebSocket framing、会话授权与协议级背压仍是
  下一项 M4 能力。
