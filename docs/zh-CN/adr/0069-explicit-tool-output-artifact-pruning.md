# ADR 0069：显式清理工具输出 artifact 的生命周期

## 状态

接受用于 Stage5BP.

## 背景

Stage5BK 将已脱敏且有界的工具输出 artifact 作为应用状态目录下的私有文件保存.
Stage5BM 从持久化的工具终态事件 metadata 推导会话可见性,Stage5BN/Stage5BO
通过 TUI 和 CLI 提供有界读取.这些文件不是 SQLite 行,因此删除、fork、导入或导出会话时
不会自动删除它们.

## 决策

提供显式的 `sessions artifacts --prune` CLI 操作.应用服务先通过 `SessionStore`
扫描所有持久化会话,只保留工具终态事件引用的合法 artifact ID,再委托基础设施垃圾回收端口.
文件适配器只删除不在完整引用集合中且超过一小时宽限期的规范 artifact 文件.

清理会保留已引用文件、近期文件、格式异常的文件、符号链接、非普通文件,以及清理过程中
已经消失的文件.命令只返回有界的删除/保留数量,不暴露原始输出、参数、绝对路径、secret
或事件 payload.

## 边界

- 清理只能显式触发;删除会话、fork、导入、导出、启动、普通 Runtime 回合都不会删除 artifact.
- 引用扫描和文件 unlink 是分离的 best-effort 操作;本阶段不宣称 SQLite 与文件系统之间存在原子事务.
- 不新增 schema、事件类型、Provider、Finalizer、权限、Sandbox、TUI 布局或 ACP wire 契约.
- 非法持久化 metadata 不加入保留集合;适配器的规范文件名和年龄检查仍是最终删除边界.

## 放弃的方案

- 在 `SessionStore.delete_session()` 中自动删除: SQLite 事务无法原子地拥有无关的文件系统文件.
- 启动时或每个回合后自动删除: 会使保留策略隐式化,并可能与读取或恢复竞态.
- fork/导入/导出时复制 artifact: 需要新的可移植 artifact 契约,并可能泄露本地诊断输出.
- 删除所有无法识别的 `.log`: 格式异常文件和符号链接应保留以便人工检查.
