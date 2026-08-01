# ruff: noqa: RUF001  # Chinese display copy and the intentional angle prompt marker.
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static


class NeuroCodeMonoDemo(App[None]):
    """Neuro Code 极简黑白界面演示。"""

    TITLE: ClassVar[str] = "Neuro Code"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+l", "clear_chat", "清屏"),
        Binding("ctrl+k", "command_palette", "命令"),
        Binding("m", "cycle_model", "模型"),
        Binding("e", "cycle_effort", "强度"),
    ]

    CSS: ClassVar[str] = r"""
    Screen {
        background: #0c0c0c;
        color: #e7e7e7;
        layout: vertical;
    }

    /* 顶部：无边框、无色块，只保留轻量信息 */
    #header {
        height: 3;
        padding: 1 2 0 2;
        background: #0c0c0c;
    }

    #brand {
        width: auto;
        height: 1;
        color: #f2f2f2;
        text-style: bold;
    }

    #header-space {
        width: 1fr;
    }

    #clock {
        width: auto;
        height: 1;
        color: #626262;
        text-align: right;
    }

    /* 主内容区 */
    #main {
        height: 1fr;
        padding: 0 3;
        background: #0c0c0c;
    }

    #conversation {
        height: 1fr;
        padding: 2 1 1 1;
        background: #0c0c0c;
        color: #dedede;
        scrollbar-size: 1 1;
    }

    /* 底部输入区域：只有一条分隔线，不使用大边框 */
    #composer {
        height: auto;
        padding: 0 3 1 3;
        background: #0c0c0c;
    }

    #context-line {
        height: 2;
        padding: 0 1;
        border-top: solid #252525;
        color: #707070;
        align-vertical: middle;
    }

    #context-left {
        width: auto;
        height: 1;
    }

    #context-space {
        width: 1fr;
    }

    #context-right {
        width: auto;
        height: 1;
        text-align: right;
    }

    #prompt-row {
        height: 3;
        padding: 0 1;
        background: #111111;
        border-left: tall #3a3a3a;
        align-vertical: middle;
    }

    #prompt-mark {
        width: 3;
        height: 1;
        color: #f0f0f0;
        text-style: bold;
        content-align: center middle;
    }

    #prompt {
        width: 1fr;
        height: 1;
        padding: 0;
        margin: 0;
        border: none;
        background: #111111;
        color: #eeeeee;
    }

    #prompt > .input--placeholder {
        color: #555555;
    }

    #prompt-row:focus-within {
        border-left: tall #f0f0f0;
    }

    #footer {
        height: 2;
        padding: 1 1 0 1;
        color: #555555;
    }

    /* 通知保持低调 */
    Toast {
        background: #181818;
        color: #d8d8d8;
        border: solid #333333;
    }
    """

    MODELS: ClassVar[tuple[str, ...]] = (
        "deepseek-v4-flash",
        "gpt-5.6",
        "claude-sonnet",
    )
    EFFORTS: ClassVar[tuple[str, ...]] = ("low", "medium", "high")

    def __init__(self) -> None:
        super().__init__()
        self.model_index = 0
        self.effort_index = 1
        self.workspace = str(Path.cwd())

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static(id="brand")
            yield Static(id="header-space")
            yield Static(id="clock")

        with Vertical(id="main"):
            yield RichLog(
                id="conversation",
                markup=False,
                wrap=True,
                highlight=False,
                auto_scroll=True,
            )

        with Vertical(id="composer"):
            with Horizontal(id="context-line"):
                yield Static(id="context-left")
                yield Static(id="context-space")
                yield Static(id="context-right")

            with Horizontal(id="prompt-row"):
                yield Static("›", id="prompt-mark")
                yield Input(
                    placeholder="描述你想完成的任务",
                    id="prompt",
                )

            yield Static(id="footer")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._update_clock)
        self._update_clock()
        self._refresh_chrome()
        self._write_empty_state()
        self.query_one("#prompt", Input).focus()

    def _refresh_chrome(self) -> None:
        model = self.MODELS[self.model_index]
        effort = self.EFFORTS[self.effort_index]

        brand = Text()
        brand.append("NEURO", style="bold #f2f2f2")
        brand.append(" / CODE", style="#777777")
        self.query_one("#brand", Static).update(brand)

        left = Text()
        left.append(model, style="#bdbdbd")
        left.append("  ·  ", style="#444444")
        left.append(effort, style="#8a8a8a")
        left.append("  ·  ", style="#444444")
        left.append("normal", style="#666666")
        self.query_one("#context-left", Static).update(left)

        right = Text()
        right.append(self.workspace, style="#666666")
        self.query_one("#context-right", Static).update(right)

        footer = Text()
        items = (
            ("^Q", "退出"),
            ("^L", "清屏"),
            ("^K", "命令"),
            ("M", "模型"),
            ("E", "强度"),
        )
        for index, (key, label) in enumerate(items):
            if index:
                footer.append("    ")
            footer.append(key, style="bold #a8a8a8")
            footer.append(f" {label}", style="#555555")
        self.query_one("#footer", Static).update(footer)

    def _update_clock(self) -> None:
        self.query_one("#clock", Static).update(datetime.now(tz=UTC).astimezone().strftime("%H:%M"))

    def _write_empty_state(self) -> None:
        log = self.query_one("#conversation", RichLog)
        log.clear()

        log.write("")
        title = Text()
        title.append("Neuro Code", style="bold #eaeaea")
        log.write(title)

        subtitle = Text()
        subtitle.append(
            "终端中的编程智能体",
            style="#646464",
        )
        log.write(subtitle)
        log.write("")

        hint = Text()
        hint.append("输入任务开始工作", style="#8a8a8a")
        hint.append("，或键入 ", style="#555555")
        hint.append("/help", style="#bdbdbd")
        hint.append(" 查看命令。", style="#555555")
        log.write(hint)

        examples = Text()
        examples.append("\n例如：", style="#454545")
        examples.append(
            "\n  修复当前项目的测试失败\n  分析这个模块的性能瓶颈\n  为现有接口补充单元测试",
            style="#666666",
        )
        log.write(examples)

    @on(Input.Submitted, "#prompt")
    def handle_prompt_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        if not prompt:
            return

        log = self.query_one("#conversation", RichLog)

        user = Text()
        user.append("YOU", style="bold #9b9b9b")
        user.append("\n")
        user.append(prompt, style="#eeeeee")
        log.write("")
        log.write(user)

        assistant = Text()
        assistant.append("NEURO", style="bold #e7e7e7")
        assistant.append("\n")
        assistant.append(
            "这是极简黑白界面的模拟回复。这里可以接入模型流式输出、工具调用过程以及代码修改结果。",
            style="#c8c8c8",
        )
        log.write("")
        log.write(assistant)

        event.input.value = ""

    def action_clear_chat(self) -> None:
        self._write_empty_state()
        self.notify("已清空")

    def action_command_palette(self) -> None:
        self.query_one("#prompt", Input).value = "/"
        self.query_one("#prompt", Input).focus()

    def action_cycle_model(self) -> None:
        self.model_index = (self.model_index + 1) % len(self.MODELS)
        self._refresh_chrome()
        self.notify(self.MODELS[self.model_index])

    def action_cycle_effort(self) -> None:
        self.effort_index = (self.effort_index + 1) % len(self.EFFORTS)
        self._refresh_chrome()
        self.notify(f"强度：{self.EFFORTS[self.effort_index]}")


if __name__ == "__main__":
    NeuroCodeMonoDemo().run()
