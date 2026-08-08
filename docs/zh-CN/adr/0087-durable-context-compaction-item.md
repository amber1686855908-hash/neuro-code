# ADR 0087：持久化上下文压缩条目与恢复投影

- 状态：Stage5DG 纵向切片已接受
- 日期：2026-08-08
- 范围：应用记忆、会话领域值对象和 SQLite 会话存储

## 背景

Stage5DD–5DF 已定义确定性压缩评估、Provider 感知摘要请求以及有界脱敏摘要输入.
这些阶段刻意不调用 Provider、不修改 `ModelContext`、也不持久化摘要. 后续 Runtime
接入需要一个可持久化边界,在不暴露源会话的情况下拒绝过期摘要.

## 决策

`DurableCompactionItem` 是由 `neuro_code.domain.conversation.compaction` 拥有的精简领域值.
它只保存有界 Provider 身份、上下文容量、源条目计数、半开候选索引、不透明 SHA-256 源指纹、
摘要 token 元数据、带时区创建时间以及已经脱敏的摘要. 摘要不进入该值对象的 `repr`;
提示词、工具参数/结果、凭据、完整源上下文和监督器状态永不持久化.

`neuro_code.application.memory.compaction` 中的 `build_durable_compaction_item()` 在构造领域值前完成
最终脱敏、控制字符清理、UTF-8 字节限制和摘要 token 限制. 指纹只用于过期源校验,绝不会发送给模型或界面.

`CompactionResumeRebuilder` 是显式且无副作用的投影. 它要求源计数、Provider 来源匹配,范围不重叠,
并且当前源指纹一致. 每个经过验证的范围会被内存中的 `SyntheticReason.COMPACTION_SUMMARY` 用户消息替换,
同时保留上下文来源和推理强度. 它不会运行模型、重放工具、写入存储或修改输入上下文.

SQLite schema v13 新增外键关联 `sessions` 的 `session_compaction_items`,并对每个会话的源范围增加唯一约束.
条目按 `compaction_id` 插入并支持幂等重复保存,按确定性顺序加载,会话删除时级联清理,且刻意不随分叉复制或进入导入/导出.
schema 从 v12 只向前迁移,并继续处于现有串行初始化事务中.

存储方法加入现有 `SessionStore` 端口,因此 SQLite 仍隐藏在应用端口之后,接口层不会直接读取数据库.

## 后果

- 没有压缩记录的既有会话仍然有效,重建结果保持不变.
- 本切片不改变 Provider 请求、自动压缩、Runtime 事件或主循环行为.
- 后续 Runtime 接入必须用明确事务边界持久化规范上下文替换和摘要记录;本 ADR 不声称整轮原子性.
- 当源上下文发生变化后进行连续压缩,后续接入必须保存新的规范源快照;重建器会拒绝源计数不匹配、重叠或过期记录,不会猜测.
