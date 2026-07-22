# ADR 0044：仓库级技能发现

**简体中文** · [English](../../en/adr/0044-repository-level-skill-discovery.md)

- 状态：已接受
- 日期：2026-07-22

## 背景

当工作区是仓库子目录时，仓库技能和 monorepo 包级技能可能位于工作区之上，原有扫描
无法看到它们。

## 决策

通过有界、纯文件系统的向上搜索检测最近 git 根，标志是常规 `.git` 目录或文件。
链接/reparse point 不作为仓库标志。测试仍可显式传入 `git_root`。

REPO 范围扫描工作区上方的每个祖先，直到并包含 git 根，按离工作区近到远处理。因此
中间 monorepo 层级和 git 根都会发现。所有 REPO 相对路径共用 git 根边界，保持路径
唯一并让技能工具安全地重新打开。较近的同名 REPO 技能覆盖 git 根默认技能；总体
优先级仍为 `LOCAL > REPO > USER`。

## 影响

- ACP/TUI 事件循环中不会启动 `git` 子进程。
- worktree 的 `.git` 文件可作为标志，但不解析其引用目标；裸仓库和非标准布局不会推断。
- 仓库祖先遍历共用全局发现预算和 64 层上限。
- Server、Bundled 和 Plugin 范围仍未实现。
