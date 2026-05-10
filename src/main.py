from __future__ import annotations

import os
import sys
import subprocess
import platform
import time
from pathlib import Path

import typer
from rich.console import Console

# 核心导入
from src.cli.base import app, console, PROJECT_ROOT
from src.utils.config import ensure_workspace, load_config

# 显式导入命令模块以触发 Typer 注册
import src.cli.commands_wiki
import src.cli.commands_dev

@app.command()
def chat(
    trace: bool = typer.Option(False, help="显示工具执行追踪"),
    stream: bool = typer.Option(False, help="流式渲染输出"),
) -> None:
    """启动 WikiCoder 智能对话终端 (Textual GUI)。"""
    from src.ui.app import WikiCoderApp
    from src.core.wikicoder_engine import BuildAgent
    config = load_config()
    WikiCoderApp(config, lambda cfg: BuildAgent(cfg)).run()

@app.command()
def serve(
    action: str = typer.Argument("run", help="动作: run, start, stop, status"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
):
    """管理 WikiCoder 后端 Web 服务。"""
    pid_file = PROJECT_ROOT / "wikicoder.pid"
    
    def is_running(pid):
        try:
            if platform.system() == "Windows":
                res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
                return str(pid) in res.stdout
            else:
                os.kill(pid, 0)
                return True
        except: return False

    if action == "status":
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            if is_running(pid):
                console.print(f"[bold green]● WikiCoder 服务正在运行 (PID: {pid})[/bold green]")
                return
        console.print("[bold red]○ WikiCoder 服务未运行[/bold red]")
    
    elif action == "stop":
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            if is_running(pid):
                if platform.system() == "Windows":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
                else:
                    import signal
                    os.kill(pid, signal.SIGTERM)
            pid_file.unlink(missing_ok=True)
            console.print("[bold green]服务已停止。[/bold green]")
            
    elif action == "start":
        log_file = PROJECT_ROOT / "wikicoder_server.log"
        
        # [关键映射]：在 Windows 下优先尝试寻找 pythonw.exe (无窗口版本)
        executable = sys.executable
        if platform.system() == "Windows":
            pw = Path(executable).parent / "pythonw.exe"
            if pw.exists():
                executable = str(pw)
        
        cmd = [executable, __file__, "serve", "run", "--host", host, "--port", str(port)]
        
        if platform.system() == "Windows":
            import subprocess
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0 # SW_HIDE
            
            process = subprocess.Popen(
                cmd, 
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                startupinfo=si,
                stdout=open(log_file, "a"), 
                stderr=subprocess.STDOUT, 
                cwd=PROJECT_ROOT,
                close_fds=True
            )
        else:
            process = subprocess.Popen(cmd, preexec_fn=os.setsid, stdout=open(log_file, "a"), stderr=subprocess.STDOUT, cwd=PROJECT_ROOT)
        
        pid_file.write_text(str(process.pid))
        console.print(f"[bold green]服务已在后台静默启动 (PID: {process.pid})[/bold green]")
        
    else: # run
        from src.core.web_api import start_server
        console.print(f"[bold green]服务启动中...[/bold green] http://{host}:{port}")
        pid_file.write_text(str(os.getpid()))
        try: start_server(host=host, port=port)
        finally: pid_file.unlink(missing_ok=True)

def run_cli():
    if len(sys.argv) <= 1:
        chat(trace=False, stream=False)
    else:
        app()

if __name__ == "__main__":
    run_cli()
