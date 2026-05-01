import os
import json
from datetime import datetime
from typing import Any, Callable

# 核心逻辑导入
from src.main import (
    run_sync, kb_save, save_kb_backup, list_kb_backups, 
    restore_kb_backup, _set_vault_path, _set_model_config,
    convert_pdf_path, convert_docx_path, convert_xlsx_path
)
from src.utils.db_manager import clear_index_store
from src.utils.config import load_config

class TUIDispatcher:
    """TUI 指令执行中心：负责所有 / 指令的业务分发"""
    
    @staticmethod
    def execute(root: str, arg: str, app_instance: Any, log_func: Callable[[str], None]):
        """执行耗时或复杂的后台指令"""
        try:
            if root == "/sync":
                res = run_sync()
                log_func(f"[green]Sync Done: changed={res['files']} chunks={res['chunks']} wiki_pages={res.get('wiki_pages', 0)}[/green]")
            
            elif root == "/kbpath":
                ok, msg = _set_vault_path(arg)
                log_func(f"[{'green' if ok else 'red'}]{msg}[/]")
            
            elif root == "/kbclear":
                cfg = load_config()
                msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
                log_func("\n".join([f"[yellow]{m}[/yellow]" for m in msgs]))
            
            elif root == "/kbbackups":
                items = list_kb_backups(limit=10)
                if not items: log_func("No backups found.")
                else: log_func("\n".join([f"- {it['id']} | {it['created_at']}" for it in items]))
            
            elif root == "/kbrestore":
                ok, msgs = restore_kb_backup(load_config(), arg)
                log_func("\n".join([f"[cyan]{m}[/cyan]" for m in msgs]))
            
            elif root == "/memsave":
                from src.main import _save_memory_markdown
                history = app_instance.session_history
                summary = "\n".join([f"## Q: {q}\n{a}\n" for q, a in history])
                path = _save_memory_markdown(app_instance.config, arg or "session", summary)
                log_func(f"[bold green]Saved to Wiki: {path}[/bold green]")
            
            elif root == "/pdf2md":
                outs, errs = convert_pdf_path(arg, recursive=True)
                log_func(f"PDF Convert: Done={len(outs)}, Errors={len(errs)}")
            
            elif root == "/docx2md":
                outs, errs = convert_docx_path(arg, recursive=True)
                log_func(f"Docx Convert: Done={len(outs)}, Errors={len(errs)}")
            
            elif root == "/xlsx2md":
                outs, errs = convert_xlsx_path(arg, recursive=True)
                log_func(f"Excel Convert: Done={len(outs)}, Errors={len(errs)}")
                
            elif root == "/undo":
                # 这里可以扩展 undo 逻辑
                log_func("[yellow]Undo feature is being integrated...[/yellow]")
            
            else:
                log_func(f"[red]Command logic not implemented in dispatcher: {root}[/red]")
                
        except Exception as e:
            log_func(f"[bold red]Command Failed:[/bold red] {str(e)}")
