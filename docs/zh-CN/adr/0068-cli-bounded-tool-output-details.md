# ADR 0068：CLI 会话作用域的有界工具输出详情

## 状态

Stage5BO 接受.

## 背景

Stage5BK 只在本地工具预览被截断时保存已脱敏、有界的工具输出 artifact.
Stage5BM 增加会话作用域应用服务,通过持久化工具终态事件证明不透明句柄的归属.
Stage5BN 已将该服务用于 TUI 展开,但无头模式用户仍无法查看有界输出.

## 决策

为 CLI 增加 `sessions artifacts SESSION_ID [ARTIFACT_ID]`.

- 不提供 artifact ID 时,列出当前会话关联 artifact 的有界元数据页.
- 提供 artifact ID 时,只能通过 `SessionToolOutputArtifactApplicationService`
  和调用方给出的有界字节上限读取已关联 artifact.
- JSON 只输出不透明 ID、字节数、截断事实、事件序号,以及必要时的已脱敏有界内容.
- 不渲染相对文件系统路径、状态目录、原始 metadata、工具参数或存储异常文本.
- `FileToolOutputArtifactStore` 由 bootstrap 持有; CLI 只接收类型化应用服务.

## 边界

这是只读入站切片.不新增 schema、事件、Runtime 写入、artifact 删除、保留策略或 ACP
协议字段.缺失或未关联句柄继续通过应用层通用会话错误边界处理.

## 放弃的方案

- 在 `cli.py` 中直接读取 `state_dir/tool-output`: 会绕过会话关联校验并泄露基础设施细节.
- 在 `sessions list` 中加入 artifact 内容: 会引入意外 I/O,并使现有会话目录查询无界.
- 新增持久化表: 现有工具终态事件 metadata 已足以支持本有界读取用例的会话关联.
