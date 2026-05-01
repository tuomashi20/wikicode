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
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Header, Footer, Input, Static, Label, Tree, RichLog, Button, ListItem, ListView, TextArea
from textual.reactive import reactive
from textual.message import Message
from rich.text import Text
from rich.panel import Panel
from rich.markdown import Markdown

# 模拟之前的配置结构
from src.core.build_agent import BuildStep

class AgentStepMessage(Message):
    """自定义消息：用于从后台线程向 UI 传递 Agent 步进信息"""
    def __init__(self, step: BuildStep) -> None:
        self.step = step
        super().__init__()

class AgentLogMessage(Message):
    """自定义消息：用于传递原始日志信息"""
    def __init__(self, text: str, style: str = "white") -> None:
        self.text = text
        self.style = style
        super().__init__()

class WikiInput(TextArea):
    """自定义多行输入框，支持特定的按键逻辑"""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.language = "markdown"

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

class WikiCoderApp(App):
    """WikiCoder v3.2: 全功能工业级 Textual 交互界面"""
    
    CSS = """
    Screen {
        background: #0f0f0f;
        layers: base popup;
    }
    
    #main-container {
        layout: grid;
        grid-size: 2;
        grid-columns: 75% 25%;
        height: 1fr;
    }
    
    #history-panel {
        background: #0f0f0f;
        overflow-x: hidden;
    }
    
    #main-log {
        overflow-x: hidden;
    }
    
    #sidebar {
        border-left: solid #222;
        background: #111;
        padding: 1;
    }
    
    #input-section {
        height: 5;
        background: #161616;
        border-top: solid #333;
        padding: 0 1;
    }
    
    #user-input {
        height: 3;
        border: none;
        background: transparent;
    }
    
    #status-bar {
        height: 1;
        margin-top: 0;
        layout: horizontal;
    }
    
    .status-dot {
        color: #22c55e;
        text-style: bold;
        margin-right: 1;
    }
    
    #status-text {
        color: #d1d5db;
        width: 25;
    }

    #loading-dots {
        color: #06b6d4;
        text-style: bold;
    }
    
    #interrupt-hint {
        color: #666;
        margin-left: 2;
    }
    
    #command-popup {
        display: none;
        dock: bottom;
        layer: popup;
        width: 80%;
        height: 14;
        background: #1c1c1c;
        border: solid #333;
        margin-bottom: 7; 
        margin-left: 2;
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
        color: #00ff00;
    }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+l", "clear_history", "清空"),
        ("tab", "toggle_mode", "切换模式"),
        ("escape", "stop_task", "终止任务"),
    ]

    session_mode = reactive("plan")
    is_processing = reactive(False)
    loading_dots = reactive("...")
    
    input_history: list[str] = []
    history_index: int = -1
    menu_stage = reactive(0) 
    current_parent_cmd = reactive("")
    _ignore_input_change = False 

    COMMAND_HELP = {
        "/sync": "同步知识库",
        "/kbpath": "设置库路径",
        "/mode": "切换模式",
        "/model": "切换模型",
        "/reset": "重置会话",
        "/resume": "恢复会话",
        "/kbclear": "清除索引",
        "/kbbackups": "备份列表",
        "/kbrestore": "恢复备份",
        "/undo": "撤销写入",
        "/version": "查看版本",
        "/help": "命令手册",
        "/exit": "退出 WikiCoder"
    }

    WIKI_COMMANDS = list(COMMAND_HELP.keys())

    COMMAND_METADATA = {
        "/mode": ["plan", "build"],
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

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        
        with Container(id="main-container"):
            with VerticalScroll(id="history-panel"):
                yield RichLog(id="main-log", highlight=True, markup=True, wrap=True)
            
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
        self.log_area = self.query_one("#main-log", RichLog)
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
        mode_str = "Build" if self.session_mode == "build" else "Plan"
        status = f"[bold white]{mode_str}[/bold white] · [dim]{model_name}[/dim]"
        self.status_dot.styles.color = "#fbbf24" if self.is_processing else "#22c55e"
        self.status_text.update(status)

    def watch_session_mode(self, mode: str):
        self.update_status_bar()
        self.log_area.write(f"\n[cyan]System: Switched to {mode.upper()} mode[/cyan]")

    def action_submit(self) -> None:
        if self.is_processing:
            self.log_area.write("[yellow]Busy... Press ESC to stop.[/yellow]"); return
        raw_cmd = self.input_field.text.strip()
        if not raw_cmd: return
        if not self.input_history or self.input_history[-1] != raw_cmd: self.input_history.append(raw_cmd)
        self.history_index = -1; self.input_field.text = ""; self.query_one("#command-popup").styles.display = "none"
        if raw_cmd.startswith("/"): self.route_command(raw_cmd); return
        self.log_area.write(f"\n[bold cyan]You:[/bold cyan] {raw_cmd}"); self.is_processing = True
        self.current_worker = self.run_agent_task(raw_cmd)

    def route_command(self, cmd: str):
        parts = cmd.split(); root = parts[0].lower(); arg = cmd[len(root):].strip()
        
        # 1. 处理 UI 状态同步指令（立即执行）
        if root == "/mode":
            if arg in ["plan", "build"]: 
                self.session_mode = arg
                from src.main import _save_session_state
                _save_session_state(self.session_history, mode=self.session_mode)
            else: self.log_area.write("[yellow]Usage: /mode plan|build[/yellow]")
        elif root == "/reset":
            self.log_area.clear(); self.agent = None; self.session_history = []
            from src.main import _clear_session_state_file; _clear_session_state_file()
            self.log_area.write("[cyan]System: Conversation reset.[/cyan]")
        elif root == "/resume":
            from src.main import _load_session_state
            h, m = _load_session_state()
            if h:
                self.session_history = h; self.session_mode = m
                self.log_area.write(f"[cyan]System: Resumed {len(h)} turns.[/cyan]")
            else: self.log_area.write("[yellow]No session state found to resume.[/yellow]")
        elif root == "/model":
            if not arg: self.log_area.write("[yellow]Usage: /model <name>[/yellow]")
            else:
                from src.main import _set_model_config; ok, msg = _set_model_config(arg)
                self.log_area.write(f"[{'green' if ok else 'red'}]{msg}[/]")
                if ok: 
                    self.config = __import__("src.utils.config", fromlist=["load_config"]).load_config()
                    self.agent = None; self.update_status_bar()
        elif root == "/version": self.log_area.write("[bold cyan]WikiCoder Pro TUI v3.2.0[/bold cyan]")
        elif root == "/exit": self.exit()
        elif root == "/help":
             self.log_area.write("\n[bold cyan]Command Help:[/bold cyan]\n" + "\n".join([f" {k:12} - {v}" for k, v in self.COMMAND_HELP.items()]))
        else:
            # 2. 委托给后台调度器的耗时指令
            self.log_area.write(f"\n[bold magenta]Running Command:[/bold magenta] {cmd}")
            self.is_processing = True
            self.current_worker = self.run_background_cmd(root, arg)

    @work(exclusive=True, thread=True)
    def run_background_cmd(self, root: str, arg: str = ""):
        from src.ui.dispatcher import TUIDispatcher
        TUIDispatcher.execute(root, arg, self, lambda m: self.post_message(AgentLogMessage(m)))
        self.is_processing = False

    def action_toggle_mode(self): self.session_mode = "build" if self.session_mode == "plan" else "plan"
    def action_stop_task(self):
        if self.current_worker: self.current_worker.cancel(); self.is_processing = False

    @work(exclusive=True, thread=True)
    def run_agent_task(self, query: str) -> None:
        try:
            if not self.agent: self.agent = self.agent_factory(self.config)
            def on_step(step): self.post_message(AgentStepMessage(step)); return True
            rep = self.agent.run(query, history=self.session_history, on_step=on_step, mode=self.session_mode)
            self.session_history.append((query, rep)); self.post_message(AgentLogMessage(f"\n[green]Report:[/green]\n{rep}"))
        except Exception as e: self.post_message(AgentLogMessage(f"[red]Error: {e}[/red]"))
        finally: self.is_processing = False

    @on(AgentStepMessage)
    def handle_agent_step(self, message: AgentStepMessage) -> None:
        s = message.step
        self.log_area.write(f"\n[magenta]Thought:[/magenta] {s.thought}\n[cyan]Action:[/cyan] {s.action_type}")
        if s.tasks:
            self.task_tree.clear()
            for t in s.tasks: self.task_tree.root.add_leaf(t)
            self.task_tree.root.expand()

    @on(AgentLogMessage)
    def handle_log_message(self, message: AgentLogMessage) -> None: self.log_area.write(message.text)
    def action_clear_history(self) -> None: self.log_area.clear()

if __name__ == "__main__":
    from src.utils.config import load_config; from src.core.build_agent import BuildAgent
    WikiCoderApp(load_config(), lambda cfg: BuildAgent(cfg)).run()
