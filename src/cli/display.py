import time
from rich.markdown import Markdown
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.console import RenderableType
from rich.box import ROUNDED, DOUBLE
from rich.align import Align
from src.cli.base import console, CLI_BANNER
from src.utils.config import AppConfig
from src.skills.code_tools import summarize_unified_diff
from src.core.llm_client import global_stats

class LiveUI:
    """OpenCode 风格的动态布局管理器"""
    def __init__(self, config: AppConfig, mode: str):
        self.config = config
        self.mode = mode
        self.history_items: list[tuple[str, str]] = []
        self.current_response = ""
        self.current_thought = ""
        self.current_steps: list[str] = []
        self.tasks: list[str] = []
        self.layout = Layout()
        self._init_layout()

    def _init_layout(self):
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=3)
        )
        self.layout["body"].split_row(
            Layout(name="main", ratio=3),
            Layout(name="side", ratio=1)
        )

    def _make_header(self) -> Panel:
        grid = Table.grid(expand=True)
        grid.add_column(justify="left", ratio=1)
        grid.add_column(justify="right", ratio=1)
        grid.add_row(
            f"[bold cyan]WIKICODER[/bold cyan] [dim]v2.0[/dim]",
            f"[bold magenta]Mode:[/bold magenta] {self.mode} | [bold blue]LLM:[/bold blue] {self.config.llm.model}"
        )
        return Panel(grid, style="white on blue", box=ROUNDED)

    def _make_side_panel(self) -> RenderableType:
        task_table = Table(title="[bold yellow]任务清单[/bold yellow]", box=ROUNDED, expand=True)
        task_table.add_column("状态", width=4)
        task_table.add_column("任务描述")
        
        for t in self.tasks:
            if "[x]" in t: task_table.add_row("[green]✔[/green]", t.replace("[x]", "").strip())
            elif "[/]" in t: task_table.add_row("[yellow]▶[/yellow]", t.replace("[/]", "").strip())
            else: task_table.add_row("[dim]○[/dim]", t.replace("[ ]", "").strip())

        stats_table = Table(title="[bold cyan]系统状态[/bold cyan]", box=ROUNDED, expand=True)
        stats_table.add_column("指标")
        stats_table.add_column("值")
        total_t = global_stats.total_prompt_tokens + global_stats.total_completion_tokens
        stats_table.add_row("Tokens", f"{total_t}")
        stats_table.add_row("Cost", f"${global_stats.total_cost:.4f}")
        
        return Layout().split(
            Layout(Panel(task_table, box=ROUNDED)),
            Layout(Panel(stats_table, box=ROUNDED))
        )

    def _make_main_panel(self) -> Panel:
        from rich.console import Group
        elements: list[RenderableType] = []
        
        for q, a in self.history_items[-3:]:
            elements.append(f"[bold cyan]You:[/bold cyan] {q}")
            elements.append(Markdown(a))
            elements.append("[dim]" + "─" * 40 + "[/dim]")
        
        if self.current_response or self.current_thought or self.current_steps:
            elements.append("[bold green]Agent:[/bold green]")
            if self.current_thought:
                elements.append(Panel(f"[dim]{self.current_thought}[/dim]", title="Thought", border_style="dim"))
            
            if self.current_steps:
                step_table = Table.grid(padding=(0, 1))
                for s in self.current_steps[-5:]:
                    step_table.add_row("[yellow]●[/yellow]", f"[dim]{s}[/dim]")
                elements.append(step_table)
            
            if self.current_response:
                elements.append(Markdown(self.current_response))

        return Panel(Group(*elements) if elements else Align.center("等待指令输入...", vertical="middle"), title=" 交互历史 ", box=ROUNDED, border_style="cyan")

    def _make_footer(self) -> Panel:
        return Panel(f"[dim]输入 /help 查看命令 | 路径: {self.config.wiki_strategy.raw_path}[/dim]", box=ROUNDED)

    def __rich__(self) -> Layout:
        self.layout["header"].update(self._make_header())
        self.layout["main"].update(self._make_main_panel())
        self.layout["side"].update(self._make_side_panel())
        self.layout["footer"].update(self._make_footer())
        return self.layout

def _stream_markdown(text: str, enabled: bool = True, delay: float = 0.006) -> None:
    if not enabled:
        console.print(Markdown(text))
        return
    current = ""
    with Live(Markdown(""), console=console, refresh_per_second=20) as live:
        for ch in text:
            current += ch
            live.update(Markdown(current))
            if delay > 0: time.sleep(delay)

def _print_startup_banner() -> None:
    console.clear()
    console.print(Align.center(f"[bold cyan]{CLI_BANNER}[/bold cyan]"))
    console.print(Align.center("[bold cyan]wikicoder cli[/bold cyan]"))

def _print_trace(resp_thought: str, resp_actions: list[str]) -> None:
    console.print(f"[dim]thought:[/dim] {resp_thought}")
    for a in resp_actions:
        console.print(f"[dim]- {a}[/dim]")

def _print_patch_preview(patch_text: str) -> None:
    items = summarize_unified_diff(patch_text)
    if not items:
        console.print("[yellow]No parseable unified diff found in output.[/yellow]")
        return
    console.print("[cyan]Patch preview:[/cyan]")
    for it in items:
        console.print(f"- {it.file or '(unknown)'} | hunks={it.hunks} +{it.added} -{it.removed}")

def _print_runtime_settings(config: AppConfig, *, session_mode: str) -> None:
    llm = config.llm
    ws = config.wiki_strategy
    console.print(
        "[dim]"
        f"provider={llm.provider} | text_model={llm.model} | session_mode={session_mode}\n"
        f"raw={ws.raw_path} | wiki={ws.wiki_path}"
        "[/dim]"
    )

def _replay_session_on_screen(history: list[tuple[str, str]]) -> None:
    if not history: return
    console.print("[cyan]—— 已恢复历史对话 ——[/cyan]")
    for q, a in history:
        console.print(f"[bold cyan]You:[/bold cyan] {q}")
        console.print(Markdown(a))
    console.print("[cyan]—— 历史对话结束 ——[/cyan]")
