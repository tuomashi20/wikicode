import os
import sys
import re
import json
import threading
import queue
import time
import ast
import subprocess
from datetime import datetime
from pathlib import Path

import typer
import yaml
from rich.live import Live
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.box import ROUNDED
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings

try:
    import msvcrt
except Exception:
    msvcrt = None
try:
    import ctypes
except Exception:
    ctypes = None
try:
    import select
    import termios
    import tty
except Exception:
    select = None
    termios = None
    tty = None

from src.cli.base import console, SESSION_STATE_PATH, build_agent, build_llm, CLI_BANNER
from src.cli.display import (
    LiveUI,
    _print_startup_banner,
    _print_runtime_settings,
    _stream_markdown,
    _print_trace,
    _print_patch_preview,
    _replay_session_on_screen,
)
from src.utils.config import load_config, ensure_workspace, AppConfig, PROJECT_ROOT, DEFAULT_CONFIG_PATH
from src.skills.code_tools import read_file, backup_and_apply_single, backup_and_apply_multi, summarize_unified_diff
from src.core.agent import AgentResponse, WikiFirstAgent
from src.core.build_agent import BuildAgent, BuildStep
from src.core.llm_client import LLMClient, global_stats

class SlashCommandCompleter(Completer):
    def __init__(self):
        self.cmd_tree = {
            "/vaultpath": "设置知识库根目录",
            "/sync": "执行同步",
            "/structure": "查看索引结构",
            "/kbclear": "清空索引",
            "/kbbackups": "查看知识库备份",
            "/kbsave": "备份知识库",
            "/kbrestore": "恢复知识库",
            "/mode": {"plan": "规划模式", "build": "构建模式"},
            "/model": {"jiutian-think-v3": "思考模型", "jiutian-lan-comv3": "对话模型"},
            "/resume": "恢复上次会话",
            "/reset": "清空会话记忆",
            "/archive": "总结并存档到 Wiki",
            "/ask": "强制 Wiki 提问",
            "/memdraft": "整理会话草稿",
            "/memsave": "保存草稿到 Wiki",
            "/xlsx2md": "Excel 转 MD",
            "/pdf2md": "PDF 转 MD",
            "/docx2md": "Word 转 MD",
            "/patch": "生成单文件补丁",
            "/patchm": "生成多文件补丁",
            "/backups": "代码备份列表",
            "/undo": "撤销代码修改",
            "/exit": "退出",
        }

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"): return
        parts = text.split()
        if len(parts) <= 1 and not text.endswith(" "):
            query = parts[0] if parts else ""
            for cmd, info in self.cmd_tree.items():
                if cmd.startswith(query):
                    desc = info if isinstance(info, str) else "包含二级选项..."
                    yield Completion(cmd, start_position=-len(query), display_meta=desc)

def build_key_bindings() -> KeyBindings:
    kb = KeyBindings()
    @kb.add("c-c")
    def _(event): event.app.exit()
    @kb.add("escape")
    def _(event):
        buf = event.app.current_buffer
        if buf.complete_state: buf.cancel_completion()
    @kb.add("enter")
    def _(event):
        buf = event.app.current_buffer
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
        buf.validate_and_handle()
    return kb

def _escape_pressed() -> bool:
    if os.name == "nt":
        try:
            if ctypes is not None and bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000):
                return True
        except: pass
        if msvcrt is None: return False
        pressed = False
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\x1b",): pressed = True
        return pressed
    if os.name != "nt" and select is not None:
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0)
            if r:
                return os.read(sys.stdin.fileno(), 1) == b"\x1b"
        except: pass
    return False

def _enable_posix_cbreak_if_needed():
    if os.name == "nt" or termios is None or tty is None: return None
    try:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return (fd, old)
    except: return None

def _restore_posix_terminal(state) -> None:
    if state and termios:
        try:
            fd, old = state
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except: pass

def _run_agent_with_thinking(agent, user_input, force_wiki, ui=None, mode="auto", code_context="", response_mode="answer", target_file="", history=None, silent=False):
    state = {}
    token_q = queue.Queue()
    status_q = queue.Queue()
    def _work():
        try:
            state["resp"] = agent.run(user_input, force_wiki=force_wiki, mode=mode, code_context=code_context, response_mode=response_mode, target_file=target_file, history=history, on_token=token_q.put, on_status=status_q.put)
        except Exception as e: state["err"] = e
    t = threading.Thread(target=_work, daemon=True)
    t.start()
    start = time.perf_counter()
    term_state = _enable_posix_cbreak_if_needed()
    try:
        with Live(ui if ui else "", console=console, refresh_per_second=10, transient=True) as live:
            while t.is_alive() or not token_q.empty():
                while not status_q.empty():
                    st = status_q.get()
                    if ui:
                        ui.current_steps.append(st)
                    else:
                        console.print(f"[dim]step: {st}[/dim]")
                
                chunks = []
                while not token_q.empty(): chunks.append(token_q.get())
                if chunks:
                    chunk_text = "".join(chunks)
                    if ui:
                        ui.current_response += chunk_text
                    elif not silent:
                        console.print(chunk_text, end="")
                
                if _escape_pressed(): return AgentResponse(thought="cancelled", actions=[], output="已取消")
                time.sleep(0.05)
    finally: _restore_posix_terminal(term_state)
    if "err" in state: raise state["err"]
    resp = state["resp"]
    if ui:
        ui.current_thought = resp.thought
        ui.tasks = getattr(resp, "tasks", [])
    return resp

def _run_llm_with_thinking(llm, system_prompt, user_prompt, phase="处理中"):
    state = {}
    def _work():
        try: state["text"] = llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as e: state["err"] = e
    t = threading.Thread(target=_work, daemon=True); t.start()
    start = time.perf_counter()
    with Live("", console=console, transient=True) as live:
        while t.is_alive():
            live.update(f"[bold cyan] {phase} {time.perf_counter()-start:.1f}s[/bold cyan]")
            if _escape_pressed(): return ""
            time.sleep(0.1)
    if "err" in state: raise state["err"]
    return state.get("text", "").strip()

def _save_session_state(history, mode):
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"mode": mode, "history": [{"q": q, "a": a} for q, a in history[-30:]]}
        SESSION_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except: pass

def _load_session_state():
    if not SESSION_STATE_PATH.exists(): return [], "plan"
    try:
        data = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        return [(r["q"], r["a"]) for r in data.get("history", [])], data.get("mode", "plan")
    except: return [], "plan"

def _clear_wiki_output(wiki_path: Path) -> list[str]:
    messages: list[str] = []
    wiki_dir = Path(wiki_path)
    if not wiki_dir.exists(): return [f"Wiki dir not found: {wiki_dir}"]
    for file_path in sorted([p for p in wiki_dir.rglob("*") if p.is_file()], key=lambda p: len(p.parts), reverse=True):
        try:
            file_path.unlink()
            messages.append(f"Removed wiki file: {file_path}")
        except Exception as e:
            try:
                file_path.write_text("", encoding="utf-8")
                messages.append(f"Truncated locked wiki file: {file_path}")
            except Exception as e2:
                messages.append(f"Failed clearing wiki file {file_path}: {e}; {e2}")
    return messages

def chat_repl(trace: bool = False, stream: bool = False):
    ensure_workspace()
    config = load_config()
    _print_startup_banner()
    session_history, session_mode = _load_session_state()
    agent = build_agent(config)
    session = PromptSession(completer=SlashCommandCompleter(), key_bindings=build_key_bindings())
    
    ui = LiveUI(config, session_mode)
    ui.history_items = session_history
    
    while True:
        try:
            # 清理上一轮的临时状态
            ui.current_response = ""
            ui.current_thought = ""
            ui.current_steps = []
            
            text = session.prompt(">>> ").strip()
        except (KeyboardInterrupt, EOFError): break
        if not text: continue
        if text in {"/exit", "/quit"}: break
        
        if text.startswith("/"):
            if text == "/help":
                console.print(Panel("Slash Commands:\n" + "\n".join([f"{k}: {v}" for k,v in SlashCommandCompleter().cmd_tree.items()]), box=ROUNDED))
            elif text.startswith("/mode"):
                parts = text.split()
                if len(parts) > 1:
                    session_mode = parts[1]
                    ui.mode = session_mode
                    console.print(f"[green]Mode set to {session_mode}[/green]")
            else:
                console.print(f"[yellow]命令尚未完全支持：{text}[/yellow]")
            continue
        
        resp = _run_agent_with_thinking(agent, text, force_wiki=False, ui=ui, history=session_history, mode=session_mode)
        
        session_history.append((text, resp.output))
        ui.history_items = session_history
        _save_session_state(session_history, session_mode)
