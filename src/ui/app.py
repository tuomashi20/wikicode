import asyncio
import os
import json
import time
import platform
import subprocess
import base64
from datetime import datetime
from typing import Optional, List, Set, Iterable, Dict, Any, Callable

from textual import on, work, events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Input, Static, Label, Tree, RichLog, Button, ListItem, ListView, TextArea
from textual.reactive import reactive
from textual.message import Message
from textual.screen import Screen
from rich.text import Text
from rich.panel import Panel
from rich.markdown import Markdown
from rich.style import Style
from rich.theme import Theme

# 定义 WikiCoder 专属的清爽配色主题
WIKICODER_THEME = Theme({
    "markdown.h1": "bright_cyan bold",
    "markdown.h2": "bright_cyan bold underline",
    "markdown.h3": "bright_cyan bold",
    "markdown.h4": "yellow bold",
    "markdown.strong": "white bold",
    "markdown.italic": "magenta italic",
    "markdown.block_quote": "bright_black italic",
    "markdown.hr": "cyan",
    "markdown.code": "bright_green",
    "markdown.code_block": "bright_white on #1e1e1e",
    "table.header": "bright_cyan bold",
    "table.footer": "bright_cyan bold",
    "table.title": "bright_white bold",
    "table.caption": "dim",
})

class StyledMarkdown(Markdown):
    """支持自定义主题的 Markdown 渲染器，兼容旧版 rich"""
    def __rich_console__(self, console, options):
        with console.use_theme(WIKICODER_THEME):
            yield from super().__rich_console__(console, options)

# 模拟之前的配置结构
from src.core.types import BuildStep
from src.core.constants import CORE_COMMANDS

class BuildStepMessage(Message):
    """自定义消息：用于从后台线程向 UI 传递 Agent 步进信息"""
    def __init__(self, step: BuildStep) -> None:
        self.step = step
        super().__init__()

class AgentLogMessage(Message):
    """自定义消息：用于传递原始日志信息或 Rich 可渲染对象"""
    def __init__(self, content: Any, style: str = "white") -> None:
        self.content = content
        self.style = style
        super().__init__()

class ReaderScreen(Screen):
    BINDINGS = [
        Binding("c", "copy_all", "复制全文"),
        Binding("escape,q", "pop_screen", "关闭"),
    ]

    def __init__(self, content: str):
        super().__init__()
        self.content = content

    def compose(self) -> ComposeResult:
        yield Header()
        # 直接使用带 Markdown 语法高亮的 TextArea，实现划选与阅读的统一
        # 暂时关闭高亮，防止由于环境缺失 tree-sitter-markdown 导致崩溃
        yield TextArea(self.content, id="reader-raw", read_only=True, language=None)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#reader-raw").focus()

    def action_copy_all(self) -> None:
        try:
            import pyperclip
            pyperclip.copy(self.content)
            self.notify("内容已全部复制到剪贴板", title="📋 复制成功")
        except Exception as e:
            self.notify(f"复制失败: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()

class WikiInput(TextArea):
    """自定义多行输入框，支持特定的按键逻辑"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 移除强制的 markdown 语言设置，防止 tree-sitter 缺失报错
        self.show_line_numbers = False 

    def action_undo(self) -> None:
        """安全撤销：防止 Textual 在文档缩减时光标越界崩溃"""
        try:
            super().action_undo()
        except ValueError:
            self.cursor_location = (0, 0)

    def action_redo(self) -> None:
        """安全重做：防止 Textual 光标同步异常"""
        try:
            super().action_redo()
        except ValueError:
            self.cursor_location = (0, 0)

    def _on_key(self, event) -> None:
        popup = self.app.query_one("#command-popup")
        is_popup_open = popup.styles.display == "block"

        if is_popup_open:
            cmd_list = self.app.query_one("#cmd-list", ListView)
            if event.key == "up":
                cmd_list.action_cursor_up()
                event.stop(); event.prevent_default(); return
            elif event.key == "down":
                cmd_list.action_cursor_down()
                event.stop(); event.prevent_default(); return
            elif event.key == "enter":
                if cmd_list.index is not None:
                    cmd_list.action_select_cursor()
                    event.stop(); event.prevent_default(); return
            elif event.key == "escape":
                popup.styles.display = "none"
                event.stop(); event.prevent_default(); return
        else:
            if event.key == "escape":
                # 让 Esc 键冒泡到 App 级别触发 stop_task 绑定
                return
            if event.key == "up":
                if self.app.input_history:
                    if self.app.history_index == -1:
                        self.app.history_index = len(self.app.input_history) - 1
                    elif self.app.history_index > 0:
                        self.app.history_index -= 1
                    self.text = self.app.input_history[self.app.history_index]
                    self.cursor_location = (0, len(self.text))
                event.stop(); event.prevent_default(); return
            elif event.key == "down":
                if self.app.input_history:
                    if self.app.history_index != -1:
                        if self.app.history_index < len(self.app.input_history) - 1:
                            self.app.history_index += 1
                            self.text = self.app.input_history[self.app.history_index]
                        else:
                            self.app.history_index = -1
                            self.text = ""
                        self.cursor_location = (0, len(self.text))
                event.stop(); event.prevent_default(); return

        if event.key == "tab":
            self.app.action_toggle_mode()
            event.stop(); event.prevent_default(); return

        if event.key == "enter":
            event.stop(); event.prevent_default()
            self.app.action_submit()
        elif event.key == "shift+enter" or event.key == "ctrl+j":
            self.insert("\n")
            event.stop(); event.prevent_default()

class AskUserScreen(Screen):
    """[工业级交互] 决策确认弹窗，支持按钮选择与开放输入"""
    def __init__(self, question: str, options: List[str] = None):
        super().__init__()
        self.question = question
        self.options = options or ["是", "否"]

    def compose(self) -> ComposeResult:
        with Vertical(id="ask-user-container"):
            yield Label("🎯 决策请求", id="ask-user-title")
            yield Static(self.question, id="ask-user-question")
            
            with Horizontal(id="ask-user-options"):
                for i, opt in enumerate(self.options):
                    yield Button(opt, variant="primary", id=f"opt-{i}")
            
            yield Label("或者在下方补充详细指令:", id="ask-user-hint")
            yield Input(placeholder="输入自定义答复...", id="ask-user-input")
            yield Label("按 Enter 确认自定义输入 | 点击按钮直接选择", id="ask-user-footer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # 提取按钮文本
        res = str(event.button.label)
        self.dismiss(res)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.dismiss(event.value.strip())

class WikiCoderApp(App):
    """WikiCoder v3.2: 全功能工业级 Textual 交互界面"""
    
    CSS = """
    Screen {
        background: #0B0C10;
        layers: base popup;
    }
    
    #main-container {
        layout: grid;
        grid-size: 2;
        grid-columns: 70% 30%;
        height: 1fr;
        padding: 1 2;
    }
    
    #history-panel {
        background: transparent;
        overflow-y: scroll;
        padding-right: 2;
    }
    
    #main-log {
        overflow-x: hidden;
    }
    
    #sidebar {
        border: solid #1F2833;
        background: #111418 60%;
        padding: 1 2;
        margin-left: 2;
    }
    
    #input-section {
        height: 8;
        background: #161B22;
        border-top: tall #45A29E;
        padding: 0 3;
    }
    
    #user-input {
        height: 4;
        border: none;
        background: transparent;
        color: #66FCF1;
    }
    
    #status-bar {
        height: 1;
        margin-top: 0;
        layout: horizontal;
    }
    
    .status-dot {
        color: #66FCF1;
        text-style: bold;
    }
    
    #status-text {
        color: #8892B0;
        width: 40;
    }

    #cwd-text {
        color: #444;
        margin-left: 2;
    }

    #mouse-hint {
        color: #66FCF1;
        margin-left: 2;
    }

    #loading-dots {
        color: #66FCF1;
        text-style: bold;
    }
    
    #interrupt-hint {
        color: #444;
        margin-left: 2;
    }
    
    #command-popup {
        display: none;
        dock: bottom;
        layer: popup;
        width: 50%;
        height: 14;
        background: #1F2833;
        border: thick #66FCF1;
        margin-bottom: 9; 
        margin-left: 5;
    }
    
    #command-popup ListView {
        background: transparent;
    }
    
    #command-popup ListItem {
        padding: 0 1;
        height: 1;
        layout: horizontal;
    }
    
    .cmd-name {
        color: #fff;
        text-style: bold;
    }

    .cmd-desc {
        color: #888;
        padding-left: 2;
    }
    
    #command-popup ListItem:hover {
        background: #333;
    }

    #task-tree {
        background: transparent;
        color: #66FCF1;
    }

    /* 决策弹窗样式 */
    #ask-user-container {
        width: 60%;
        height: auto;
        max-height: 25;
        background: #111418;
        border: thick #66FCF1;
        padding: 1 3;
        align: center middle;
    }

    #ask-user-title {
        color: #66FCF1;
        text-style: bold;
        margin-bottom: 1;
        text-align: center;
    }

    #ask-user-question {
        background: #1F2833 20%;
        padding: 1 2;
        margin-bottom: 1;
        color: #C5C6C7;
        border: round #45A29E;
    }

    #ask-user-options {
        height: 4;
        align: center middle;
        margin-bottom: 1;
    }

    #ask-user-options Button {
        margin: 0 1;
        background: #1F2833;
        color: #66FCF1;
        border: tall #66FCF1;
    }

    #ask-user-options Button:hover {
        background: #45A29E;
        color: #0B0C10;
    }

    #ask-user-hint {
        color: #8892B0;
        margin-top: 1;
    }

    #ask-user-input {
        background: #0B0C10;
        border: solid #45A29E;
        color: #66FCF1;
        margin-bottom: 1;
    }

    #ask-user-footer {
        color: #444;
        text-style: italic;
        text-align: center;
    }

    .message-user {
        background: #45A29E 20%;
        margin: 1 0 1 15;
        padding: 1 2;
        border: round #66FCF1;
        width: 80%;
        align-horizontal: right;
    }

    .message-bot {
        background: #111418;
        margin: 1 10 1 0;
        padding: 1 2;
        border-left: thick #66FCF1;
        width: 85%;
    }

    .message-system {
        color: #444;
        margin: 0 6;
        text-style: italic;
        text-align: center;
    }
    
    VerticalScroll {
        scrollbar-size: 1 1;
        scrollbar-color: #45A29E;
        scrollbar-color-hover: #66FCF1;
        scrollbar-background: transparent;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        Binding("ctrl+l", "clear_screen", "清空", show=True),
        Binding("escape", "stop_task", "终止任务", show=True),
        Binding("ctrl+v", "open_reader", "双击文本划选", show=True),
        Binding("ctrl+p", "palette", "命令面板", show=False),
    ]

    session_mode = reactive("chat")
    is_processing = reactive(False)
    loading_dots = reactive("...")
    
    input_history: list[str] = []
    history_index: int = -1
    menu_stage = reactive(0) 
    current_parent_cmd = reactive("")
    _ignore_input_change = False 

    COMMAND_HELP = CORE_COMMANDS

    WIKI_COMMANDS = list(COMMAND_HELP.keys())

    COMMAND_METADATA = {
        "/mode": ["chat", "agent"],
        "/model": ["jiutian-think-v3", "jiutian-lan-comv3"],
        "/kbbackups": ["list", "clean"],
    }

    def __init__(self, config, agent_factory):
        super().__init__()
        self.config = config
        self.agent_factory = agent_factory
        self.agent = None
        self.current_worker = None
        self.session_history = []
        self.input_history = []
        self.history_index = -1
        self.modified_files: Set[str] = set()
        self.initial_cwd = __import__('os').getcwd() # 记录启动目录
        self._last_click_time = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Container(id="main-container"):
            with VerticalScroll(id="history-panel"):
                # 这里不再使用 RichLog，而是动态挂载 Static 组件
                yield Static("[dim]Welcome to WikiCoder. Type /help for commands.[/dim]\n", id="init-msg", classes="message-system")
            
            with Vertical(id="sidebar"):
                yield Label("[bold cyan]Quick check-in[/bold cyan]", variant="title")
                yield Tree("项目任务清单", id="task-tree")
                yield Label("\n[bold yellow]Modified Files[/bold yellow]")
                yield ListView(id="file-list")
        
        with Vertical(id="input-section"):
            yield WikiInput(id="user-input")
            with Horizontal(id="status-bar"):
                yield Label("●", classes="status-dot", id="status-dot")
                yield Label("Mode: ", id="status-text")
                yield Label(" 🖱️ 双击文本划选", id="mouse-hint")
                yield Label("", id="cwd-text") 
                yield Label("", id="loading-dots")
                yield Label("", id="interrupt-hint")

        with Vertical(id="command-popup"):
            yield Horizontal(
                Label("[bold cyan] ⚡ Commands[/bold cyan]", id="popup-title"),
                classes="popup-header"
            )
            yield ListView(id="cmd-list")
        
        yield Footer()

    def _update_loading_animation(self) -> None:
        try:
            dots_label = self.query_one("#loading-dots", Label)
            hint_label = self.query_one("#interrupt-hint", Label)
            if self.is_processing:
                dots = getattr(self, "loading_dots", "...")
                if len(dots) >= 10: dots = "."
                else: dots += "."
                self.loading_dots = dots
                dots_label.update(f"[bold cyan]{dots}[/]")
                hint_label.update("  [dim]esc interrupt[/]")
            else:
                dots_label.update("")
                hint_label.update("")
        except: pass

    def on_mount(self) -> None:
        self.history_panel = self.query_one("#history-panel", VerticalScroll)
        self.task_tree = self.query_one("#task-tree", Tree)
        self.file_list = self.query_one("#file-list", ListView)
        self.input_field = self.query_one("#user-input", WikiInput)
        self.status_text = self.query_one("#status-text", Label)
        self.status_dot = self.query_one("#status-dot", Label)
        self.task_tree.root.expand()
        self.input_field.focus()
        self.update_status_bar()
        self.set_interval(0.3, self._update_loading_animation)

    @on(TextArea.Changed, "#user-input")
    def on_input_changed(self, event: TextArea.Changed) -> None:
        if self._ignore_input_change: return
        text = event.text_area.text
        popup = self.query_one("#command-popup")
        if text.startswith("/"):
            if " " in text:
                parts = text.split(maxsplit=1)
                cmd_root = parts[0].lower()
                if cmd_root in self.COMMAND_METADATA:
                    popup.styles.display = "block"
                    self.menu_stage = 1
                    self.current_parent_cmd = cmd_root
                    query = parts[1].lower() if len(parts) > 1 else ""
                    self.refresh_menu_items(self.COMMAND_METADATA[cmd_root], query, f"{cmd_root}")
                else: popup.styles.display = "none"
            else:
                popup.styles.display = "block"
                self.menu_stage = 0
                self.refresh_menu_items(self.WIKI_COMMANDS, text.lower(), "Commands")
            cmd_list = self.query_one("#cmd-list", ListView)
            if len(cmd_list.query(ListItem)) == 0: popup.styles.display = "none"
            elif cmd_list.index is None: cmd_list.index = 0
        else: popup.styles.display = "none"; self.menu_stage = 0

    def refresh_menu_items(self, items: List[str], query: str, title: str):
        cmd_list = self.query_one("#cmd-list", ListView)
        title_label = self.query_one("#popup-title", Label)
        title_label.update(f"[bold cyan] ⚡ {title}[/bold cyan]")
        target_options = [cmd for cmd in items if not query or query in cmd.lower()]
        cmd_list.clear()
        for cmd in target_options:
            desc = self.COMMAND_HELP.get(cmd, "") if self.menu_stage == 0 else ""
            item = ListItem(Label(cmd, classes="cmd-name"), Label(desc, classes="cmd-desc"), name=cmd)
            cmd_list.append(item)

    @on(ListView.Selected, "#cmd-list")
    def on_cmd_selected(self, event: ListView.Selected) -> None:
        selected_text = str(event.item.name or "")
        self._ignore_input_change = True
        popup = self.query_one("#command-popup")
        popup.styles.display = "none"
        if self.menu_stage == 0 and selected_text in self.COMMAND_METADATA:
            self.menu_stage = 1
            self.current_parent_cmd = selected_text
            self.input_field.text = selected_text + " "
            self._ignore_input_change = False 
            self.refresh_menu_items(self.COMMAND_METADATA[selected_text], "", f"{selected_text}")
            popup.styles.display = "block"; self.input_field.focus()
            self.input_field.cursor_location = (0, len(self.input_field.text))
        else:
            full_cmd = f"{self.current_parent_cmd} {selected_text}" if self.menu_stage == 1 else selected_text
            self.input_field.text = full_cmd; self.menu_stage = 0; self.current_parent_cmd = ""
            self.input_field.focus(); self.input_field.cursor_location = (0, len(self.input_field.text))
            async def unlock_soon(): await asyncio.sleep(0.1); self._ignore_input_change = False
            asyncio.create_task(unlock_soon())

    def update_status_bar(self):
        try: model_name = getattr(self.config.llm, 'model', '未知')
        except: model_name = "未知"
        mode_str = "Agent" if self.session_mode == "agent" else "Chat"
        
        # 获取当前工作目录
        current_path = self.agent.cwd if (self.agent and hasattr(self.agent, 'cwd')) else self.initial_cwd
        
        self.status_text.update(f"Mode: [bold #66FCF1]{mode_str}[/] | Model: [#C5C6C7]{model_name}[/]")
        
        self.status_dot.styles.color = "#fbbf24" if self.is_processing else "#22c55e"


    def watch_session_mode(self, mode: str):
        self.update_status_bar()
        self.append_message("system", f"System: Switched to {mode.upper()} mode")

    def action_submit(self) -> None:
        if self.is_processing:
            self.append_message("system", "[yellow]Busy... Press ESC to stop.[/yellow]"); return
        raw_cmd = self.input_field.text.strip()
        if not raw_cmd: return
        if not self.input_history or self.input_history[-1] != raw_cmd: self.input_history.append(raw_cmd)
        self.history_index = -1; self.input_field.text = ""; self.query_one("#command-popup").styles.display = "none"
        if raw_cmd.startswith("/"): self.route_command(raw_cmd); return
        
        processed_cmd = raw_cmd
            
        self.append_message("user", raw_cmd)
        self.is_processing = True
        self.current_worker = self.run_agent_task(processed_cmd)

    def append_message(self, role: str, content: Any = "") -> Static:
        """向消息流中添加一个新的消息块"""
        if role == "user":
            new_msg = Static(Text.assemble(("\n You: ", "bold cyan"), f"{content}\n"), classes="message-user")
        elif role == "system":
            new_msg = Static(f"{content}", classes="message-system")
        else:
            # Bot 消息，使用支持自定义样式的 StyledMarkdown
            new_msg = Static(StyledMarkdown(content) if content else "", classes="message-bot")
        
        self.history_panel.mount(new_msg)
        # 强制滚动到底部，确保最新内容可见
        self.history_panel.scroll_end(animate=False)
        return new_msg

    def route_command(self, cmd: str):
        parts = cmd.split(); root = parts[0].lower(); arg = cmd[len(root):].strip()
        
        # 1. 处理 UI 状态同步指令（立即执行）
        if root == "/mode":
            if arg in ["chat", "agent"]: 
                self.session_mode = arg
                from src.cli.repl import _save_session_state
                _save_session_state(self.session_history, mode=self.session_mode)
            else: self.append_message("system", "[yellow]Usage: /mode plan|build[/yellow]")
        elif root == "/reset":
            # 清空 UI 历史
            for child in self.history_panel.children: child.remove()
            self.agent = None; self.session_history = []
            from src.cli.base import SESSION_STATE_PATH
            SESSION_STATE_PATH.unlink(missing_ok=True)
            self.append_message("system", "[cyan]System: Conversation reset.[/cyan]")
        elif root == "/resume":
            from src.cli.repl import _load_session_state
            h, m = _load_session_state()
            if h:
                self.session_history = h; self.session_mode = m
                # 清空初始信息
                for child in self.history_panel.children: child.remove()
                
                for q, a in h:
                    self.append_message("user", q)
                    self.append_message("bot", a)
                self.append_message("system", f"System: Resumed {len(h)} turns.")
                self.update_status_bar()
            else: self.append_message("system", "No session state found to resume.")
        elif root == "/copy": self.action_copy_last()
        elif root == "/view": self.action_open_reader()
        elif root == "/export":
            from src.cli.repl import _save_session_state
            from src.cli.base import PROJECT_ROOT
            import os
            path = PROJECT_ROOT / f"export_{__import__('datetime').datetime.now().strftime('%m%d_%H%M%S')}.md"
            summary = "\n\n".join([f"### You: {q}\n\n{a}" for q, a in self.session_history])
            path.write_text(f"# WikiCoder Export\n\n{summary}", encoding="utf-8")
            self.append_message("system", f"\n[bold green]Exported to: {path}[/bold green]")
        elif root == "/model":
            if not arg: self.append_message("system", "[yellow]Usage: /model <name>[/yellow]")
            else:
                from src.utils.config import DEFAULT_CONFIG_PATH
                import yaml
                ok, msg = False, "Config not found"
                if DEFAULT_CONFIG_PATH.exists():
                    try:
                        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
                        data.setdefault("llm", {})["model"] = arg
                        DEFAULT_CONFIG_PATH.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
                        ok, msg = True, f"Model updated to: {arg}"
                    except Exception as e: msg = str(e)
                self.append_message("system", f"[{'green' if ok else 'red'}]{msg}[/]")
                if ok: 
                    self.config = __import__("src.utils.config", fromlist=["load_config"]).load_config()
                    self.agent = None; self.update_status_bar()
        elif root == "/version": self.append_message("system", "[bold cyan]WikiCoder Pro TUI v3.2.0[/bold cyan]")
        elif root == "/exit": self.exit()
        elif root == "/help":
             self.append_message("system", "\n[bold cyan]Command Help:[/bold cyan]\n" + "\n".join([f" {k:12} - {v}" for k, v in self.COMMAND_HELP.items()]))
        else:
            # 2. 委托给后台调度器的耗时指令
            self.append_message("system", f"\n[bold magenta]Running Command:[/bold magenta] {cmd}")
            self.is_processing = True
            self.current_worker = self.run_background_cmd(root, arg)

    @work(exclusive=True, thread=True)
    def run_background_cmd(self, root: str, arg: str = ""):
        from src.ui.dispatcher import TUIDispatcher
        TUIDispatcher.execute(root, arg, self, lambda m: self.post_message(AgentLogMessage(m)))
        self.is_processing = False

    def action_toggle_mode(self): self.session_mode = "agent" if self.session_mode == "chat" else "chat"


    def action_open_reader(self):
        if not self.session_history:
            self.notify("没有历史内容可查看", severity="error")
            return
        summary = "\n\n".join([f"### You: {q}\n\n{a}" for q, a in self.session_history])
        self.push_screen(ReaderScreen(summary))

    def action_copy_last(self):
        if not self.session_history: 
            self.notify("没有可复制的历史记录", severity="error")
            return
        last_rep = self.session_history[-1][1]
        try:
            import pyperclip
            pyperclip.copy(last_rep)
            self.notify("最后一条回复已复制到剪贴板", title="📋 复制成功")
        except ImportError:
            self.notify("请先运行 'uv add pyperclip' 以启用复制功能", severity="warning")
        except Exception as e:
            self.notify(f"复制失败: {e}", severity="error")

    def action_stop_task(self):
        if self.current_worker: 
            self.current_worker.cancel()
            self.is_processing = False
            self.notify("⚠️ 正在强制终止任务...", severity="warning", title="刹车已启动")
            self.append_message("system", "System: Stop signal sent. Waiting for agent to safely exit...")

    @work(exclusive=True, thread=True)
    def run_agent_task(self, query: str) -> None:
        try:
            # --- 实时 Markdown 生长模式 ---
            # 在线程中通过 call_from_thread 安全创建 UI 组件
            bot_msg = self.call_from_thread(self.append_message, "bot", "")
            full_rep = ""

            def safe_update_ui(content):
                if bot_msg:
                    bot_msg.update(StyledMarkdown(content))
                    self.history_panel.scroll_end(animate=False)

            def on_step(step):
                # 协作式终止检查
                from textual.worker import get_current_worker
                worker = get_current_worker()
                if worker and worker.is_cancelled: return False
                
                # 发送步进消息用于更新侧边栏（不重复在日志中打印描述，由内核 on_log 处理）
                self.post_message(BuildStepMessage(step))
                return True

            def on_log(msg):
                nonlocal full_rep
                # 记录并实时更新内容
                full_rep += msg
                self.call_from_thread(safe_update_ui, full_rep)

            from textual.worker import get_current_worker
            worker = get_current_worker()

            # 懒加载检查：防止 Agent 为 None 时崩溃
            if self.agent is None:
                self.agent = self.agent_factory(self.config)


            rep = self.agent.run(
                query,
                history=self.session_history,
                on_step=on_step,
                on_log=on_log,
                on_token=on_log,  # [流式支持] 直接复用 on_log 的追加更新逻辑
                mode=self.session_mode,
                should_stop=lambda: worker.is_cancelled if worker else False
            )
            
            # --- [核心增强]：处理交互式中断 ---
            if rep == "__INTERRUPTED_WAITING_USER__":
                # 获取最后一步的交互请求数据
                last_step = self.agent.steps[-1]
                try:
                    params = json.loads(last_step.action_input)
                    question = params.get("question", "未知问题")
                    options = params.get("options", ["是", "否"])
                    
                    def handle_answer(answer):
                        if answer:
                            # 将用户选择作为新的 Query 提交，触发断点恢复
                            self.input_field.text = answer
                            self.action_submit()
                            
                    self.call_from_thread(self.push_screen, AskUserScreen(question, options), handle_answer)
                except Exception as e:
                    self.call_from_thread(self.append_message, "system", f"[red]解析交互请求失败: {e}[/red]")
                return

            self.session_history.append((query, rep))
            
            # 最终呈现结果（不再覆盖，因为 on_token 已经实现了实时生长）
            # 仅在 rep 确实有内容且 full_rep 异常为空时才做补全
            if rep and not full_rep:
                self.call_from_thread(safe_update_ui, rep)
        except Exception as e:
            self.call_from_thread(self.append_message, "system", f"[red]Error: {e}[/red]")
        finally:
            self.is_processing = False
            # 自动保存会话
            try:
                from src.cli.repl import _save_session_state
                _save_session_state(self.session_history, mode=self.session_mode)
            except: pass

    @on(BuildStepMessage)
    def handle_agent_step(self, message: BuildStepMessage) -> None:
        s = message.step
        # 注意：这里的逻辑现在主要用于更新侧边栏和任务树，日志已由流式 Markdown 承接
        # 跟踪修改的文件
        if s.action_type in ("write", "edit", "write_file"):
            try:
                params = __import__('json').loads(s.action_input)
                fp = params.get('path') or params.get('file_path', '')
                if fp:
                    self.modified_files.add(str(fp))
                    self._refresh_file_list()
            except Exception:
                pass
        # 任务面板 (增加防御性类型检查，防止非字符串任务导致 split() 崩溃)
        if hasattr(s, 'tasks') and s.tasks:
            self.task_tree.clear()
            for t in s.tasks:
                # 如果是字典（模型幻觉），尝试提取其描述性字段，否则强制转字符串
                task_label = t if isinstance(t, str) else (t.get('current') or t.get('task') or str(t))
                self.task_tree.root.add_leaf(str(task_label))
            self.task_tree.root.expand()

    @on(events.Click, ".message-user, .message-bot, .message-system, #history-panel")
    def handle_log_double_click(self, event: events.Click) -> None:
        current_time = time.time()
        if current_time - self._last_click_time < 0.5:
            self.action_open_reader()
            self._last_click_time = 0 # 重置，防止连续三击触发两次
        else:
            self._last_click_time = current_time

    @on(AgentLogMessage)
    def handle_log_message(self, message: AgentLogMessage) -> None:
        self.append_message("system", message.content)

    def action_clear_screen(self) -> None:
        for child in self.history_panel.children:
            child.remove()
        self.append_message("system", "Screen cleared.")

    def _refresh_file_list(self) -> None:
        """刷新侧边栏的修改文件列表"""
        try:
            self.file_list.clear()
            for fp in sorted(self.modified_files):
                self.file_list.append(ListItem(Label(f"📄 {fp}")))
        except Exception:
            pass

if __name__ == "__main__":
    from src.utils.config import load_config; from src.core.wikicoder_engine import BuildAgent
    WikiCoderApp(load_config(), lambda cfg: BuildAgent(cfg)).run()
