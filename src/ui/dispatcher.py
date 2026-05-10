import os
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from src.utils.config import load_config, DEFAULT_CONFIG_PATH
import yaml

# 引入重构后的 Skill 层
from src.skills.wiki_skill import sync_kb, set_vault_path, clear_kb, get_structure
from src.skills.kb_backup_skill import get_backups, create_backup, restore_backup_by_id
from src.skills.chat_archive_skill import mem_draft_archive
from src.skills.doc_tool_skill import convert_pdf_to_md, convert_xlsx_to_md, convert_docx_to_md

class TUIDispatcher:
    """TUI 指令执行中心：负责所有 / 指令的业务分发 (Skill 驱动版)"""
    
    @staticmethod
    def execute(root: str, arg: str, app_instance: Any, log_func: Callable[[str], None]):
        """执行耗时或复杂的后台指令"""
        try:
            if root == "/sync":
                # 路由到 Agent 的全自动专家同步引擎
                if not getattr(app_instance, "agent", None):
                    log_func("[yellow]正在初始化专家模型引擎...[/yellow]")
                    app_instance.agent = app_instance.agent_factory(app_instance.config)
                
                # 执行专家同步
                res = app_instance.agent.sync(on_status=log_func)
                
                if isinstance(res, dict) and "error" in res:
                    report = f"❌ 同步失败: {res['error']}"
                elif isinstance(res, dict):
                    report = f"### ✅ 知识库同步完成 (V2.0)\n\n- **处理文件**: {res.get('files', 0)} 个 (已镜像至 gbrain)\n- **跳过未变**: {res.get('skipped', 0)} 个\n- **清理删除**: {res.get('deleted', 0)} 个\n- **生成切片**: {res.get('chunks', 0)} 条\n\n> 提示：Agent 模式现已激活 **gbrain 语义引擎**，可处理深层业务逻辑。"
                else:
                    report = "✅ 同步已完成。"
                
                log_func(report)
                return {"status": "success"}
            
            elif root == "/kbpath":
                if not arg or not arg.strip():
                    log_func("[red]❌ 请指定知识库路径！用法: /kbpath <绝对路径>[/red]")
                    return {"status": "error"}
                    
                ok, msg = set_vault_path(arg)
                log_func(f"[{'green' if ok else 'red'}]{msg}[/]")
                if ok:
                    app_instance.config = load_config()
                    app_instance.agent = None
                    log_func("[dim]系统配置已重载，专家模块已重定向。[/dim]")
            
            elif root == "/kbclear":
                # 兼容多种输入格式，只要带 all 就算全量清理
                is_all = "all" in arg.lower()
                msgs = clear_kb(all_data=is_all)
                log_func("\n".join([f"[yellow]{m}[/yellow]" for m in msgs]))
            
            elif root == "/kbbackups":
                items = get_backups(limit=10)
                if not items: log_func("[yellow]未发现备份记录[/yellow]")
                else:
                    lines = [f"📅 [cyan]{it['id']}[/cyan] | {it['created_at']}" for it in items]
                    log_func("--- 知识库备份列表 ---\n" + "\n".join(lines))
            
            elif root == "/kbrestore":
                if not arg:
                    log_func("[red]❌ 请指定备份 ID (可通过 /kbbackups 查看)[/red]")
                    return
                ok, msgs = restore_backup_by_id(arg)
                log_func("\n".join([f"[cyan]{m}[/cyan]" for m in msgs]))
            
            elif root == "/archive":
                from src.skills.chat_archive_skill import archive_chat_to_md
                history = [{"q": q, "a": a} for q, a in app_instance.session_history]
                ok, path = archive_chat_to_md(history, filename=arg)
                if ok: log_func(f"[bold green]✅ 已为您生成正式全量归档: {path}[/bold green]")
                else: log_func(f"[red]❌ 归档失败: {path}[/red]")

            elif root == "/status":
                cfg = load_config()
                status = [
                    f"📊 [bold]WikiCoder 运行状态[/bold]",
                    f"- 模型: [cyan]{cfg.llm.model}[/cyan]",
                    f"- 知识库: [dim]{cfg.wiki_strategy.vault_path}[/dim]",
                    f"- 工作目录: [dim]{os.getcwd()}[/dim]",
                    f"- 当前时间: {datetime.now().strftime('%H:%M:%S')}"
                ]
                log_func("\n".join(status))

            elif root == "/version":
                log_func("[bold cyan]WikiCoder Pro TUI v4.0.0 [Dual-Core][/bold cyan]")

            else:
                log_func(f"[red]未知指令: {root}[/red]")
                
        except Exception as e:
            log_func(f"[bold red]指令执行异常:[/bold red] {str(e)}")
