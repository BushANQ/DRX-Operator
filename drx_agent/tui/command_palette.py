"""Command palette screen — browse & run slash commands (Ctrl+K)."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Header, OptionList, Static
from textual.widgets.option_list import Option

from drx_agent.event_bus import Event, EventBus, EventType


class CommandPalette(Screen[None]):
    """Slash-command picker (Ctrl+K). Enter runs the highlighted command."""

    SLASH_COMMANDS: list[tuple[str, str]] = [
        ("/scan", "启动侦察扫描"),
        ("/exploit", "启动漏洞利用"),
        ("/target", "管理目标主机信息"),
        ("/status", "查看当前系统状态"),
        ("/plan", "切换到 plan 模式（仅只读工具）"),
        ("/act", "切换到 act 模式（允许全部工具）"),
        ("/mode", "查看当前模式"),
        ("/stop", "中断当前任务"),
        ("/cancel", "中断当前任务"),
        ("/interrupt", "中断当前任务"),
        ("/dream", "触发深度上下文压缩（L6 层）"),
        ("/context", "查看上下文使用量"),
        ("/progress", "查看进度文档（9 段结构）"),
        ("/memory", "查看项目记忆（DRX.md/AGENTS.md/CLAUDE.md）"),
        ("/memory reload", "重新加载项目记忆文件"),
        ("/save", "保存当前会话"),
        ("/resume", "恢复最近保存的会话"),
        ("/help", "显示命令帮助"),
    ]

    BINDINGS = [
        Binding("escape", "pop_screen", "返回", show=True),
    ]

    DEFAULT_CSS = """
    CommandPalette {
        background: $surface;
    }
    CommandPalette #palette-title {
        height: 1;
        padding: 0 1;
        text-style: bold;
        background: $boost;
    }
    CommandPalette #palette-list {
        height: 1fr;
        padding: 0 1;
    }
    CommandPalette #palette-hint {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $boost;
    }
    """

    def __init__(self, event_bus: EventBus) -> None:
        super().__init__()
        self.event_bus = event_bus

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("命令面板 — 选择要执行的命令", id="palette-title")
        yield OptionList(id="palette-list")
        yield Static("↑/↓ 选择 · Enter 执行 · Esc 返回", id="palette-hint")

    def on_mount(self) -> None:
        palette = self.query_one("#palette-list", OptionList)
        for cmd, desc in self.SLASH_COMMANDS:
            palette.add_option(Option(f"{cmd}  ·  {desc}", id=cmd))
        palette.highlighted = 0
        palette.focus()

    def action_pop_screen(self) -> None:
        """Esc — return to the previous screen without running a command."""
        self.dismiss()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        cmd = event.option.id
        if cmd:
            self._execute_command(cmd)
        self.dismiss()

    def _execute_command(self, cmd: str) -> None:
        if cmd == "/save":
            self.event_bus.publish(Event(type=EventType.SESSION_SAVE, data={}))
        elif cmd == "/resume":
            self.event_bus.publish(Event(type=EventType.SESSION_RESTORE, data={}))
        else:
            self.event_bus.publish(Event(
                type=EventType.AGENT_MESSAGE,
                data={"text": cmd, "source": "user"},
            ))
