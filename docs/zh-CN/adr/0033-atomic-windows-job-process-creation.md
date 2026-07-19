# ADR 0033：在受控 Job 中原子创建 Windows 进程

**简体中文** · [English](../../en/adr/0033-atomic-windows-job-process-creation.md)

- 状态：已接受
- 日期：2026-07-19
- 源代码基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`

## 背景

ADR 0031 为前台和受管后台命令引入了关闭即终止的 Job 所有权。第一版实现使用标准 asyncio
子进程 API，再把返回的 PID 加入预配置 Job。这会留下一个狭窄区间：恶意或只是运行极快的
入口进程可能在加入前创建后代。

Python 公开的 `subprocess.STARTUPINFO.lpAttributeList` 支持受限继承句柄列表，但不公开
Job 列表进程属性。使用 asyncio 私有 transport、先挂起启动再事后修补所有权，或请求
breakaway，都会继续保留竞态或弱化显式容器边界。

## 决策

`windows_job_process` 平台适配器只使用标准库 `ctypes`，并惰性加载 Win32 函数。
`ProcessTree` 先创建并配置关闭即终止的 Job，再把仍由自己持有的句柄借给一次
`STARTUPINFOEXW` 创建调用。属性列表同时包含：

- `PROC_THREAD_ATTRIBUTE_JOB_LIST`，指向从创建时就必须掌控子进程的 Job；以及
- `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`，只包含可继承的空输入和选定 stdout/stderr
  管道写句柄。

`CreateProcessW` 接收可变命令行、显式 Unicode 环境块与工作目录、
`STARTF_USESTDHANDLES`、`EXTENDED_STARTUPINFO_PRESENT`、
`CREATE_UNICODE_ENVIRONMENT`、`CREATE_NEW_PROCESS_GROUP` 和
`CREATE_NO_WINDOW`。`bInheritHandles` 只因显式句柄列表已经限制继承范围而设为 true。
父端管道读句柄会在创建前改为不可继承；Job 句柄不会被继承。

Win32 进程和管道是同步的。独立读取线程向公开的 `asyncio.StreamReader` 投递数据，独立
等待线程记录直接子进程退出状态。这会保持现有前台 Bash 与受管后台进程契约，同时不依赖
asyncio 私有 Windows 子进程 transport。之后 `ProcessTree.wait()` 会继续观察 Job 的活动
进程数，而终止操作仍作用于整个 Job。

管道创建、空输入打开、属性列表初始化与更新、进程创建、流读取、等待、终止和每个受控
句柄都有明确清理。创建前失败会关闭全部管道与 Job；创建后失败会先终止并回收子进程，再
释放进程句柄。合并输出产生的别名句柄只会关闭一次。

可移植测试会验证固定宽度结构、准确的 Job 列表与句柄列表属性、受限继承、创建标志、
Unicode 环境排序、Shell 命令边界、流投影、非零状态和全部主要失败路径。现有 Windows
原生进程树与后台关闭测试现在会经过这个原子启动器执行。

## 影响

- 入口进程在 Windows Job 所有权生效前无法运行任何指令，因此原先的 spawn-to-attach
  后代逃逸窗口已经消除。
- 不支持的扩展属性、不兼容的嵌套 Job 策略或无效标准句柄继承会显式导致命令创建失败；
  不存在 `taskkill`、breakaway 或启动后回退。
- 适配器不增加运行依赖，也可以在非 Windows 平台安全导入。PR #6 的
  [CI 运行 29680149723](https://github.com/amber1686855908-hash/neuro-code/actions/runs/29680149723)
  已在 Windows 3.12 和 3.14 上成功执行原生 Job 路径。
- 非 PTY Shell 流与 ConPTY 终端会话继续拥有各自的生命周期所有者。ADR 0034 现在会为
  生产 ConPTY 复用 Job 列表创建规则，并通过共享终端端口投影；ACP 协议暴露仍是独立能力。

Job 列表行为遵循 Microsoft 的
[UpdateProcThreadAttribute 文档](https://learn.microsoft.com/zh-cn/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)，
受限继承遵循
[CreateProcessW 指南](https://learn.microsoft.com/zh-cn/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)。
历史所有权证据来自只读固定 Rust 基线中的 Job Object `ProcessGroup` 与本地终端路径。
