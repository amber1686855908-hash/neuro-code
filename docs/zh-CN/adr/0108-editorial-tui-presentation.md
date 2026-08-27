# ADR 0108：编辑式 TUI 表现与工具活动聚合

**简体中文** · [English](../../en/adr/0108-editorial-tui-presentation.md)

## 状态

已接受。

## 背景

TUI 已经能够安全、有界地保存工具详情，但仍把每次工具调用投影为视觉上独立的日志卡片。
权限判定、输出行数、工作区扫描、完成标签和长命令会与助手回答争夺注意力。永久快捷键栏和
带完整字段名的运行状态栏也让底部界面比对话本身更重。

运行时事件流、权限策略、输出 artifact 边界和会话 transcript 继续作为权威来源；本次变更
只属于表现层。

## 决策

- `NeuroCodeApp` 在连续工具生命周期事件之上持有仅供 TUI 使用的活动组投影。每次调用仍
  保留 call ID 和原地状态，但界面只用一个可见活动组汇总读取、搜索、命令、编辑及其他
  活动。可见的助手、用户、计划、状态或错误条目会结束当前活动组。
- 活动组默认折叠，工作区编辑也不例外。稳定摘要只保留成功/失败/运行中状态、有界意图或
  聚合数量、关键失败信息与耗时。按 Enter 或单击会打开只包含一个所选调用的固定高度
  Inline Peek；用上/下方向键切换调用，按 Enter 打开其独立 Tool Inspector，按 Esc 返回摘要。
  再次单击已打开的 Peek 也会收起；即使流式更新把焦点移回 Composer，Esc 仍可返回摘要。
- Inline Peek 同时受十个逻辑行的 presenter 预算和十二行 widget 最大高度约束，终端换行也
  不能让 Conversation 无限增高。tree、search、file-read、Bash 与 generic tool 使用优先读取
  metadata 的 renderer；metadata 不足时，格式化 stdout 才作为有界字面文本 fallback。正常
  allow 判定和与 `✓` 重复的“完成”标签不会进入 Summary 或 Peek。
- Tool Inspector 持有可滚动的 Output、Input、Meta 视图，每个视图均可独立复制。Output
  包含可用工作区 diff，并且只在 Inspector 打开后读取会话作用域 artifact。既有 256 KiB
  读取上限、凭据脱敏、不透明句柄与会话 ownership 校验继续作为权威边界，读取/存储截断会
  明确提示。Input 递归脱敏，Meta 采用白名单，因此不会显示 artifact 路径、artifact ID、
  任意 metadata 或异常文本。
- 已打开的 Inspector 会继续绑定所选实时调用。生命周期事件更新其 presentation 时，不会
  在 Modal 中查找 Conversation widget；Modal 活跃期间，transcript 更新始终写入持久的基础
  screen。运行中耗时刷新按活动组去重，每组每次 tick 最多刷新一次；已打开的 Peek/Inspector
  不显示这个变化中的 Summary 计时，因此跳过其周期性布局刷新。
- 界面只使用一套紧凑语义 token：三层背景、一种边框、主/次/弱化前景、一种克制交互
  accent，以及 success/warning/error 和共享间距。路径、模型和工具名不再仅因对象类型
  获得 accent 色。
- 对话、计划、活动、状态和错误块共享同一左侧阅读轴，并限制为最多 116 列。永久底部区域
  只保留 Composer 与无冗余字段名的紧凑状态行。完整快捷键栏被移除；`/help` 与 F1 继续
  提供按需发现。
- Modal 使用小/中/大三档尺寸及统一 padding 和边框。选择列表用焦点箭头与独立已选勾号，
  不再使用整行选中填充。Ultracode 保持视觉区分，并显示有界委派进度。会话复制编辑器使用
  divider，而不是第二层完整边框。

## 影响

- 长工具序列会保持为一个二级活动块，不再退化成 CI 风格日志；Conversation 永远不会同时
  渲染完整 stdout 或所有调用详情，每个安全详情仍可在 Modal 中检查。
- disclosure 只属于表现层；工具执行、权限、持久化、取消、Provider 行为与 artifact 授权
  均保持不变。
- Transcript Copy 始终使用稳定 Activity Summary，不受当前 Summary/Peek 状态影响；Inspector
  复制与之明确分离。
- 窄终端通过两个有界表格投影保留强度、模式、上下文和工作区可见性；过长模型与路径会
  使用省略号。
- F1 会显示现有本地命令参考。虽然不再永久显示快捷键标签，所有既有键盘命令仍然可用。

## 验证

Textual 无头测试覆盖聚合边界、窄终端和大型组的固定高度单选 Peek、Peek 零 artifact 读取、
Inspector 专属的会话作用域读取、脱敏与截断提示、稳定 Transcript Copy、renderer fallback、
Modal 复制、宽屏阅读宽度、窄屏状态 containment、Modal 档位、克制设置列表、独立的 Ultracode 委派状态、
运行中 Inspector 终态更新、与焦点无关的收起，以及按组去重的计时刷新。
