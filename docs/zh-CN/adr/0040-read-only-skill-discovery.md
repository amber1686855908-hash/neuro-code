# ADR 0040：有界只读技能发现

**简体中文** · [English](../../en/adr/0040-read-only-skill-discovery.md)

- 状态：已接受
- 日期：2026-07-22

## 背景

仓库指令之后的下一项纵向能力，是在不下载、不执行技能的前提下发现本地
`SKILL.md` 参考文档。完整正文不应进入每一次模型提示。

## 决策

增加 `SkillDiscovery` 端口、领域值和 `FilesystemSkillDiscovery` 适配器。适配器按
`.neuro`、`.agents`、`.grok`、`.claude` 的优先级扫描其下的 `skills/`。它以有界
frontmatter 解析 `name`、`description` 和 `when-to-use`；元数据缺失或格式错误时，
回退到目录名和正文首个散文行。frontmatter 分隔符必须独占整行。

适配器拒绝所有链接/reparse point，以及不安全或不可移植的路径。限制包括：技能树
递归深度 5、祖先遍历 64、已访问技能目录 200、单目录条目 1,000、候选 200、已加载
技能 50、单文件 64 KiB、所有已接受读取总计 512 KiB。模型目录另有 64 KiB 上限。

技能规范化名称按先见为准去重，范围优先级为 `LOCAL > REPO > USER`。发现指纹包含
完整的有界文件内容，但模型目录只接收元数据。目录作为标记为 `AVAILABLE_SKILLS`
的临时 `User` 项，并提示模型通过只读 `skill` 工具加载相关正文。

## 影响

- 技能文件只是数据，不是可执行插件。
- 简单解析器有意不实现完整 YAML。
- `.cursor` 供应商技能、条件 `paths:` 激活、服务器/内置技能、hooks、plugins 和远程
  同步仍不在范围内。
- ADR 0042–0044 在不改变该安全边界的前提下扩展初始 LOCAL 范围。
