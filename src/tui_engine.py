import asyncio
import time
import os
import subprocess
import pyperclip
import contextlib
import threading
from io import StringIO
from rich.console import Console
from rich.markup import escape
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window, FormattedTextControl, FloatContainer, Float
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.formatted_text import HTML, ANSI, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.filters import has_focus
from prompt_toolkit.mouse_events import MouseEventType, MouseButton

# 专用 Rich 渲染器与线程锁
# 专用 Rich 渲染器与线程锁
# 增加 legacy_windows 支持以更好地处理 Windows 终端编码，设置较大的宽度防止 Rich 强制换行
rich_console = Console(file=StringIO(), force_terminal=True, color_system="256", width=200, legacy_windows=True)
ansi_lock = threading.Lock()

def to_ansi(rich_text: str) -> str:
    with ansi_lock:
        with rich_console.capture() as capture:
            rich_console.print(rich_text, end="")
        return capture.get()

def system_copy(text: str):
    """底层复制逻辑"""
    if not text: return
    try:
        pyperclip.copy(text)
    except:
        try:
            # 兼容处理：使用 utf-8 写入并尝试 clip
            import subprocess
            p = subprocess.Popen(['clip'], stdin=subprocess.PIPE, shell=True)
            p.communicate(input=text.encode('utf-16')) # Windows clip 更好地支持 utf-16
        except: pass

class SimpleAnsiLexer(Lexer):
    def lex_document(self, document):
        def get_line(lineno):
            return to_formatted_text(ANSI(document.lines[lineno]))
        return get_line

class TUIApp:
    def __init__(self, config, agent, build_agent_factory, cmd_completer, key_bindings):
        self.config = config
        self.agent = agent
        self.build_agent_factory = build_agent_factory
        try:
            self.main_loop = asyncio.get_running_loop()
        except RuntimeError:
            self.main_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.main_loop)
        initial_mode = getattr(config, "mode", "plan")
        self.session_mode = initial_mode if initial_mode in ["plan", "build"] else "plan"
        self.session_history = []
        self.is_processing = False
        self.stop_requested = False
        self.elapsed_time = 0.0
        self.is_first_token = True 
        self.current_build_tasks = []  # 保存构建模式下的任务清单
        self.completed_build_tasks = set()  # 保存已完成的任务名称或索引
        self.modified_files = set()  # 保存已操作的文件路径
        self.full_history_text = to_ansi("[bold cyan]WikiCoder Professional TUI[/bold cyan]\n[dim]输入问题开始对话，或输入 /help 查看命令[/dim]\n")
        self.history_area = TextArea(read_only=True, scrollbar=True, lexer=SimpleAnsiLexer(), text=self.full_history_text)

        def make_copy_handler(text_area):
            original_handler = text_area.control.mouse_handler
            def _handler(mouse_event):
                if mouse_event.event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.RIGHT:
                    selected = text_area.buffer.copy_selection().text
                    if selected:
                        system_copy(selected)
                        self.append_text("\n[bold green]系统: [右键成功] 文字已复制到剪贴板。[/bold green]\n", is_rich=True)
                    else:
                        try:
                            paste_text = pyperclip.paste()
                            if paste_text:
                                self.input_field.buffer.insert_text(paste_text)
                        except Exception:
                            pass
                    return None
                return original_handler(mouse_event)
            return _handler
        self.history_area.control.mouse_handler = make_copy_handler(self.history_area)

        def accept_handler(buffer):
            if self.is_processing: return True
            cmd = buffer.text.strip()
            if cmd: asyncio.create_task(self.handle_input(cmd)); return False
            return True

        self.input_field = TextArea(
            height=3, 
            multiline=True, 
            wrap_lines=True,
            prompt=HTML('<b><style color="cyan">WikiCoder</style></b> <style color="gray">></style> '),
            completer=cmd_completer, 
            complete_while_typing=True, 
            accept_handler=accept_handler
        )
        
        # 增加输入框专用的按键绑定：Enter 发送，Control-J 或 Alt+Enter 换行
        input_kb = KeyBindings()
        @input_kb.add("enter")
        def _(event):
            # 模拟提交
            accept_handler(event.app.current_buffer)
        @input_kb.add("c-j")
        @input_kb.add("escape", "enter") # Alt+Enter 作为换行方案
        def _(event):
            event.app.current_buffer.insert_text("\n")
        self.input_field.key_bindings = input_kb

        self.input_field.control.mouse_handler = make_copy_handler(self.input_field)

        self.task_area = TextArea(read_only=True, scrollbar=True, lexer=SimpleAnsiLexer(), 
                                  text=to_ansi("[cyan]>> 任务清单[/cyan]\n\n[dim](未处于 Build 模式或无任务)[/dim]"),
                                  width=40)

        self.stats_control = FormattedTextControl(text=self._get_stats_text)
        
        # 建立更底层的窗口引用，绕过 TextArea 复杂的嵌套布局，提升渲染稳定性
        self.history_window = Window(content=self.history_area.control, wrap_lines=True)
        self.task_window = Window(content=self.task_area.control, wrap_lines=True, width=40, style="bg:#121212")
        
        main_split = VSplit([
            self.history_window, 
            Window(width=1, char="│", style="fg:ansigray"), 
            self.task_window
        ])
        
        content_layout = HSplit([
            main_split, 
            Window(height=1, char="─", style="fg:ansigray"), 
            self.input_field, 
            Window(content=self.stats_control, height=1, style="bg:ansigray fg:ansiblack")
        ])
        self.root_container = FloatContainer(
            content_layout, 
            floats=[Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=16))]
        )
        self.kb = key_bindings
        @self.kb.add("escape")
        def _(event):
            buf = event.app.current_buffer
            if buf.complete_state: buf.cancel_completion(); return
            if self.is_processing: 
                self.stop_requested = True
                if hasattr(self, 'current_running_agent'):
                    setattr(self.current_running_agent, '_stop_requested', True)
                self.append_text("\n[bold orange3]>> 终止输出...[/bold orange3]\n", is_rich=True)
        @self.kb.add("tab")
        def _(event): self.session_mode = "build" if self.session_mode == "plan" else "plan"; self.app.invalidate()
        @self.kb.add("c-w")
        def _(event): event.app.layout.focus_next()
        @self.kb.add(" ")
        def _(event):
            buf = event.app.current_buffer
            buf.insert_text(" ")
            text = buf.text
            if any(text == cmd + " " for cmd in ["/mode", "/model", "/kbpath", "/kbrestore", "/pdf2md", "/docx2md", "/xlsx2md", "/md2canvas", "/patch", "/review", "/undo"]):
                buf.start_completion(select_first=False)
        self.app = Application(layout=Layout(self.root_container, focused_element=self.input_field), key_bindings=self.kb, full_screen=True, mouse_support=True)

    def _get_stats_text(self):
        mode_label = self.session_mode.upper() if self.session_mode in ["plan", "build"] else "PLAN"
        color = "ansiblue" if mode_label == "PLAN" else "ansimagenta"
        try:
            model_name = getattr(self.config.llm, 'model', '未知模型')
        except:
            model_name = "未知模型"
        cwd = os.getcwd()
        if len(cwd) > 25: cwd = "..." + cwd[-22:]
        cwd_safe = escape(cwd)
        model_safe = escape(model_name)
        return HTML(f'<style bg="{color}" fg="ansiwhite"><b> {mode_label} </b></style> <style fg="ansicyan">🤖 {model_safe}</style> | <style fg="gray">📂 {cwd_safe}</style> | [Enter:发送] | [Ctrl+J:换行] | [ESC:终止] | Tab:切换')

    def _update_task_panel(self):
        if self.session_mode != "build":
            text = to_ansi("[cyan]>> 任务清单[/cyan]\n\n[dim](请先切换到 Build 模式)[/dim]")
        elif not getattr(self, 'all_seen_tasks', []):
            text = to_ansi("[cyan]>> 任务清单[/cyan]\n\n[dim](等待 AI 规划任务...)[/dim]")
        else:
            out = "[cyan]>> 任务清单[/cyan]\n\n"
            for t in self.all_seen_tasks:
                safe_t = escape(t)
                if t in self.completed_tasks_text:
                    out += f"[dim gray][x] [strike]{safe_t}[/strike][/dim gray]\n"
                else:
                    out += f"[bold green][ ] {safe_t}[/bold green]\n"
            
            if self.modified_files:
                out += "\n[yellow]>> 已操作文件[/yellow]\n\n"
                for f in sorted(self.modified_files):
                    # 只显示文件名，悬停或完整路径暂不处理
                    out += f"  - [dim gray]{escape(os.path.basename(f))}[/dim gray]\n"
                    
            text = to_ansi(out)
            
        def _set():
            self.task_area.text = text
            self.app.invalidate()
        try:
            self.main_loop.call_soon_threadsafe(_set)
        except Exception:
            pass

    def append_text(self, text: str, is_rich: bool = False):
        ansi_part = to_ansi(text) if is_rich else text
        def _update():
            if not hasattr(self, 'history_frozen_text'):
                self.history_frozen_text = self.history_area.text
            self.history_frozen_text += ansi_part
            self.history_area.text = self.history_frozen_text
            self.history_area.buffer.cursor_position = len(self.history_frozen_text)
            self.app.invalidate()
        try:
            self.main_loop.call_soon_threadsafe(_update)
        except Exception:
            pass

    async def _timer_task(self):
        start_time = time.time()
        while self.is_processing:
            t = time.time() - start_time
            def _refresh():
                if not hasattr(self, 'history_frozen_text'): return
                timer_msg = to_ansi(f"\n[dim italic]⌛ AI 正在思考并执行... ({t:.1f}s)[/dim italic]")
                self.history_area.text = self.history_frozen_text + timer_msg
                self.history_area.buffer.cursor_position = len(self.history_area.text)
                self.app.invalidate()
            try: self.main_loop.call_soon_threadsafe(_refresh)
            except: pass
            await asyncio.sleep(0.1)
            
        def _clear():
            if hasattr(self, 'history_frozen_text'):
                self.history_area.text = self.history_frozen_text
                self.history_area.buffer.cursor_position = len(self.history_frozen_text)
                self.app.invalidate()
        try: self.main_loop.call_soon_threadsafe(_clear)
        except: pass

    async def handle_input(self, cmd):
        if cmd.startswith("/exit"): self.app.exit(); return
        parts = cmd.split(); root_cmd = parts[0] if parts else ""
        
        output_buffer = StringIO()
        with contextlib.redirect_stdout(output_buffer):
            try:
                # --- [等级 1: 行政与系统指令] ---
                if root_cmd == "/sync":
                    from src.main import run_sync; result = await asyncio.get_event_loop().run_in_executor(None, run_sync)
                    self.append_text(f"\n[green]系统: 同步完成 (files={result.get('files', 0)})[/green]\n", is_rich=True)
                elif root_cmd == "/kbpath":
                    arg = cmd[len(root_cmd):].strip()
                    ws = self.config.wiki_strategy
                    if not arg: self.append_text(f"\n[yellow]当前路径: {getattr(ws, 'vault_path', '未设置')}[/yellow]\n", is_rich=True)
                    else: ws.vault_path = arg; self.append_text(f"\n[green]已设置主路径为: {escape(arg)}[/green]\n", is_rich=True)
                elif root_cmd == "/resume":
                    from src.main import _load_session_state; old_hist, old_mode = _load_session_state()
                    if old_hist:
                        self.session_history = old_hist; self.session_mode = old_mode if old_mode in ["plan", "build"] else "plan"
                        for q, a in old_hist: self.append_text(f"\n\n[cyan]You:[/cyan] {escape(q)}\n", is_rich=True); self.append_text(a)
                    else: self.append_text("\n[yellow]无历史记录。[/yellow]\n", is_rich=True)
                elif root_cmd == "/kbclear":
                    if cmd == "/kbclear": self.append_text("\n[yellow]请用 /kbclear yes 确认。[/yellow]\n", is_rich=True)
                    else: from src.main import clear_index_store; await asyncio.get_event_loop().run_in_executor(None, lambda: clear_index_store(self.config.wiki_strategy.processed_path)); self.append_text("\n[green]系统: 知识库已清理。[/green]\n", is_rich=True)
                elif root_cmd == "/kbbackups":
                    from src.utils.kb_backup import list_kb_backups
                    items = list_kb_backups(limit=30)
                    if not items: self.append_text("\n[yellow]未找到知识库备份。[/yellow]\n", is_rich=True)
                    else:
                        out = "\n[bold cyan]知识库备份列表:[/bold cyan]\n"
                        for it in items: out += f"  - ID: [green]{escape(it['id'])}[/green] | 时间: {escape(str(it['created_at']))}\n"
                        self.append_text(out, is_rich=True)
                elif root_cmd == "/kbrestore":
                    arg = cmd[len(root_cmd):].strip()
                    if not arg: self.append_text("\n[yellow]用法: /kbrestore <backup_id>[/yellow]\n", is_rich=True)
                    else:
                        from src.utils.kb_backup import restore_kb_backup
                        def _do_restore(): return restore_kb_backup(self.config, arg)
                        self.append_text(f"\n[cyan]正在恢复知识库 (ID: {escape(arg)})...[/cyan]\n", is_rich=True)
                        ok, msgs = await asyncio.get_event_loop().run_in_executor(None, _do_restore)
                        for m in msgs: self.append_text(f"\n[green]{escape(m)}[/green]" if m.startswith("Restored") else f"\n[yellow]{escape(m)}[/yellow]", is_rich=True)
                        if ok: self.append_text("\n[bold green]系统: 恢复成功！[/bold green]\n", is_rich=True)
                        else: self.append_text("\n[bold red]系统: 恢复完成但包含错误/警告。[/bold red]\n", is_rich=True)
                elif root_cmd == "/undo":
                    import src.main as main_mod
                    arg = cmd[len(root_cmd):].strip() or main_mod.last_backup_id
                    if not arg: self.append_text("\n[yellow]无最近的备份记录，请使用 /kbbackups 查看或手动输入 ID。[/yellow]\n", is_rich=True)
                    else:
                        from src.utils.kb_backup import restore_kb_backup
                        def _do_restore(): return restore_kb_backup(self.config, arg)
                        self.append_text(f"\n[cyan]正在回滚知识库 (ID: {escape(arg)})...[/cyan]\n", is_rich=True)
                        ok, msgs = await asyncio.get_event_loop().run_in_executor(None, _do_restore)
                        for m in msgs: self.append_text(f"\n[green]{escape(m)}[/green]" if m.startswith("Restored") else f"\n[yellow]{escape(m)}[/yellow]", is_rich=True)
                        if ok: self.append_text("\n[bold green]系统: 撤销成功！[/bold green]\n", is_rich=True)
                        else: self.append_text("\n[bold red]系统: 撤销完成但包含错误/警告。[/bold red]\n", is_rich=True)
                elif root_cmd == "/version": self.append_text("\n[bold cyan]WikiCoder Pro TUI v1.0.0[/bold cyan]\n", is_rich=True)
                
                # --- [等级 1.5: 更多 CLI 核心与兼容指令] ---
                elif root_cmd == "/help":
                    self.append_text("\n[bold cyan]WikiCoder TUI 核心命令帮助[/bold cyan]\n", is_rich=True)
                    self.append_text(
                        " [green]/sync[/green]               同步知识库 (RAW -> 索引 -> WIKI)\n"
                        " [green]/kbpath <目录>[/green]      设置知识库根目录\n"
                        " [green]/kbclear[/green]            清空知识库向量索引\n"
                        " [green]/kbbackups[/green]          查看知识库备份列表\n"
                        " [green]/kbrestore <id>[/green]     恢复指定知识库备份\n"
                        " [green]/undo[/green]               撤销上一步操作\n"
                        " [green]/memsave [标题][/green]     将当前会话整理存入文档\n"
                        " [green]/mode plan|build[/green]    切换规划/构建模式\n"
                        " [green]/model <name>[/green]       切换使用的语言模型\n"
                        " [green]/reset[/green]              清空当前会话记忆上下文\n"
                        " [green]/ask <问题>[/green]         发起强制提问 (与直接输入等同)\n"
                        " [green]/review, /patch[/green]     执行代码审阅与生成单文件补丁\n"
                        " [green]/pdf2md, /md2canvas[/green] 文件转换工具 (支持-r递归)\n"
                    )
                elif root_cmd == "/reset":
                    self.session_history = []
                    from src.main import _clear_session_state_file; _clear_session_state_file()
                    self.append_text("\n[cyan]已清空会话上下文记忆。[/cyan]\n", is_rich=True)
                elif root_cmd == "/mode":
                    arg = cmd[len(root_cmd):].strip().lower()
                    if arg in ["plan", "build"]:
                        self.session_mode = arg; self.app.invalidate()
                        self.append_text(f"\n[cyan]已切换到 {arg.upper()} 模式[/cyan]\n", is_rich=True)
                        from src.main import _save_session_state; _save_session_state(self.session_history, mode=self.session_mode)
                    else: 
                        self.append_text("\n[yellow]用法: /mode <模式>[/yellow]\n[cyan]可用模式:[/cyan]\n  - [green]plan[/green]  (规划模式：检索知识库，制定方案)\n  - [green]build[/green] (构建模式：自动探测并执行修改)\n", is_rich=True)
                elif root_cmd == "/model":
                    arg = cmd[len(root_cmd):].strip()
                    if not arg: 
                        self.append_text("\n[yellow]用法: /model <模型名>[/yellow]\n[cyan]可用模型:[/cyan]\n  - [green]jiutian-lan-comv3[/green] (对话模型)\n  - [green]jiutian-think-v3[/green]  (思考模型)\n", is_rich=True)
                    else:
                        from src.main import _set_model_config, build_agent
                        ok, msg = _set_model_config(arg)
                        self.append_text(f"\n[{'green' if ok else 'red'}]{escape(msg)}[/]\n", is_rich=True)
                        if ok:
                            self.config = __import__("src.utils.config", fromlist=["load_config"]).load_config()
                            self.agent = build_agent(self.config)
                elif root_cmd in ["/review", "/patch"]:
                    body = cmd[len(root_cmd):].strip()
                    if " :: " not in body: self.append_text(f"\n[yellow]用法: {root_cmd} <文件> :: <要求/问题>[/yellow]\n", is_rich=True)
                    else:
                        file_path, query = body.split(" :: ", 1)
                        if root_cmd == "/review":
                            from src.main import review; await asyncio.get_event_loop().run_in_executor(None, lambda: review(file_path.strip(), query.strip()))
                        elif root_cmd == "/patch":
                            from src.main import patch; await asyncio.get_event_loop().run_in_executor(None, lambda: patch(file_path.strip(), query.strip()))
                        self.append_text(f"\n[green]{root_cmd} 任务执行完毕。[/green]\n", is_rich=True)

                # --- [等级 2: 文件工具指令 (异步强力回显版)] ---
                elif root_cmd in ["/pdf2md", "/docx2md", "/xlsx2md", "/md2canvas"]:
                    arg = cmd[len(root_cmd):].strip()
                    vp = getattr(self.config.wiki_strategy, "vault_path", None)
                    recursive = False
                    
                    if root_cmd == "/md2canvas":
                        recursive = " -r" in arg or " --recursive" in arg
                        arg = arg.replace(" --recursive", "").replace(" -r", "").strip()
                        
                    if arg and not os.path.isabs(arg) and vp: 
                        arg = os.path.join(vp, arg)
                    
                    self.append_text(f"\n[cyan]系统: 正在为您执行 {root_cmd}，请稍候...[/cyan]\n", is_rich=True)
                    
                    def run_conversion():
                        if root_cmd == "/pdf2md": from src.main import convert_pdf_path; return convert_pdf_path(arg)
                        elif root_cmd == "/docx2md": from src.main import convert_docx_path; return convert_docx_path(arg)
                        elif root_cmd == "/xlsx2md": from src.main import convert_xlsx_path; return convert_xlsx_path(arg)
                        elif root_cmd == "/md2canvas": from src.skills.canvas_tools import convert_md_canvas_path; return convert_md_canvas_path(arg, recursive=recursive, use_ai=False)
                        return [], []

                    outs, errs = await asyncio.get_event_loop().run_in_executor(None, run_conversion)
                    for o in outs: self.append_text(f"\n[green]已成功生成文件: {escape(o)}[/green]\n", is_rich=True)
                    for e in errs: self.append_text(f"\n[red]操作失败: {escape(e)}[/red]\n", is_rich=True)
                    if not outs and not errs: self.append_text(f"\n[yellow]未找到目标文件或未生成输出。[/yellow]\n", is_rich=True)

                # --- [等级 3: Agent 交互指令 (含 /memsave, /ask)] ---
                elif root_cmd.startswith("/") and root_cmd not in ["/memsave", "/memdraft", "/ask"]:
                    self.append_text(f"\n[yellow]系统: 未知指令 {escape(root_cmd)}，请输入 /help 查看手册。[/yellow]\n", is_rich=True)
                else:
                    # 真正进入 AI 交互环节
                    processed_cmd = cmd
                    save_after_done = False
                    if root_cmd in ["/memsave", "/memdraft"]:
                        if not self.session_history: self.append_text("\n[yellow]记录为空。[/yellow]\n", is_rich=True); return
                        save_title = cmd[len(root_cmd):].strip()
                        if not save_title: save_title = f"FAQ_{int(time.time())}"
                        processed_cmd = f"请整理当前会话历史为 Wiki 草稿：\n" + "\n".join([f"Q:{q}\nA:{a}" for q, a in self.session_history])
                        save_after_done = True
                        self.append_text(f"\n\n[bold cyan]系统: 正在整理并保存 '{escape(save_title)}'...[/bold cyan]\n", is_rich=True)
                    else:
                        if root_cmd == "/ask": processed_cmd = cmd[len("/ask"):].strip()
                        self.append_text(f"\n\n[bold cyan]You:[/bold cyan] {escape(cmd)}\n", is_rich=True)
                        self.append_text("[dim italic]>>> AI 正在思考并检索知识库...[/dim italic]\n", is_rich=True)

                    self.is_processing = True; self.stop_requested = False; self.elapsed_time = 0.0; timer_task = asyncio.create_task(self._timer_task())
                    self.current_answer = ""; self.is_first_token = True
                    def on_token(token):
                        if self.stop_requested: raise InterruptedError("Stopped")
                        self.current_answer += token; self.append_text(token, is_rich=False)
                        
                    def on_build_step(step):
                        if self.stop_requested: raise InterruptedError("Stopped")
                        msg = f"\n[bold magenta]► Thought:[/bold magenta] [magenta]{escape(step.thought)}[/magenta]\n"
                        msg += f"[bold cyan]► Action:[/bold cyan] [cyan]{escape(step.action_type)}[/cyan]"
                        if step.action_input: 
                            msg += f"\n[dim]{escape(step.action_input[:500] + ('...' if len(step.action_input)>500 else ''))}[/dim]"
                            # 提取操作的文件路径
                            if step.action_type in ["edit_file", "write_file", "read_file", "patch_apply"]:
                                try:
                                    import json
                                    data = json.loads(step.action_input)
                                    path = data.get("path")
                                    if path: self.modified_files.add(path)
                                except: pass
                        self.append_text(msg + "\n", is_rich=True)
                        if step.action_type == "finish":
                            for t in self.all_seen_tasks:
                                self.completed_tasks_text.add(t)
                        if step.tasks:
                            import re
                            active_this_round = []
                            for t in step.tasks:
                                t = str(t)
                                is_done = False
                                clean_t = t
                                if t.lower().startswith("[x]") or t.startswith("✅"):
                                    clean_t = t[3:].strip() if t.lower().startswith("[x]") else t[1:].strip()
                                    is_done = True
                                
                                # 剔除 AI 常常加入的 "1. " "2、" "- " 等前缀，防止因前缀丢失导致被识别为两条任务
                                clean_t = re.sub(r'^(\d+[\.\-、]\s*|\-\s*)', '', clean_t).strip()
                                
                                if clean_t not in self.all_seen_tasks: 
                                    self.all_seen_tasks.append(clean_t)
                                
                                if is_done:
                                    self.completed_tasks_text.add(clean_t)
                                else:
                                    active_this_round.append(clean_t)
                                    
                            for t in self.all_seen_tasks:
                                if t not in active_this_round and t not in self.completed_tasks_text:
                                    self.completed_tasks_text.add(t)
                        self._update_task_panel()
                        return True

                    try:
                        if self.session_mode == "build" and not save_after_done:
                            agent_build = self.build_agent_factory()
                            self.current_running_agent = agent_build # 挂载实例以便 ESC 终止
                            self.all_seen_tasks = []
                            self.completed_tasks_text = set()
                            self._update_task_panel()
                            def run_build():
                                clean_hist = [(q, a[:1000] + "..." if len(a)>1000 else a) for q, a in self.session_history]
                                return agent_build.run(processed_cmd, history=clean_hist, on_step=on_build_step)
                            final_out = await asyncio.get_event_loop().run_in_executor(None, run_build)
                            self.current_answer = final_out
                            self.append_text(f"\n\n[bold green]✅ 构建完成: {escape(final_out)}[/bold green]\n", is_rich=True)
                            self.session_history.append((cmd, f"✅ 构建完成：{final_out}"))
                            from src.main import _save_session_state; _save_session_state(self.session_history, mode=self.session_mode)
                        else:
                            agent_plan = self.agent
                            self.current_running_agent = agent_plan
                            await asyncio.get_event_loop().run_in_executor(None, lambda: agent_plan.run(processed_cmd, mode=self.session_mode, on_token=on_token))
                            if save_after_done and not self.stop_requested:
                                vp = getattr(self.config.wiki_strategy, "vault_path", os.getcwd()); target_path = os.path.join(vp, "raw", "faq", f"{save_title}.md")
                                os.makedirs(os.path.dirname(target_path), exist_ok=True); 
                                with open(target_path, "w", encoding="utf-8") as f: f.write(self.current_answer)
                                self.append_text(f"\n\n[bold green]系统: [一键记忆成功] 路径: {target_path}[/bold green]\n", is_rich=True)
                            else:
                                self.session_history.append((cmd, self.current_answer))
                                from src.main import _save_session_state; _save_session_state(self.session_history, mode=self.session_mode)
                        self.append_text(f"\n[bold gray][系统: 本次耗时 {self.elapsed_time:.1f}s][/bold gray]\n", is_rich=True)
                    except InterruptedError: pass
                    except Exception as e: self.append_text(f"\n[red]错误: {e}[/red]\n", is_rich=True)
                    finally: self.is_processing = False; await timer_task

            except Exception as e: self.append_text(f"\n[red]异常: {e}[/red]\n", is_rich=True)
        self.app.invalidate()

    async def run(self):
        self.main_loop = asyncio.get_running_loop()
        await self.app.run_async()

def run_tui(config, agent, build_agent_factory, cmd_completer, key_bindings):
    try:
        app = TUIApp(config, agent, build_agent_factory, cmd_completer, key_bindings)
        asyncio.run(app.run())
    except KeyboardInterrupt:
        # 捕获 Ctrl+C，实现静默退出
        print("\n[WikiCoder] 已安全退出。")
    except Exception as e:
        # 捕获其他非预期的启动错误
        print(f"\n[WikiCoder] 意外退出: {e}")
