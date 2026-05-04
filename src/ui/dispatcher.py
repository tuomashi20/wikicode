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
                success = app_instance.agent.sync(on_status=log_func)
                
                # 读取最新结果进行最终报告
                report = "❌ 同步失败"
                output_json = Path("d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
                if output_json.exists():
                    import json
                    data = json.loads(output_json.read_text(encoding='utf-8'))
                    nodes = data.get('nodes', [])
                    rich_atoms = [n for n in nodes if n.get('type') == 'semantic_atom' and len(n.get('properties', {})) > 0]
                    report = f"### ✅ 专家级同步完成\n\n- **提炼原子**: {len(nodes)} 个\n- **结构化语义**: {len(rich_atoms)} 条\n\n> 提示：现在的检索已具备深度业务逻辑感知能力。"
                
                return {"output": report}
            
            elif root == "/kbpath":
                ok, msg = set_vault_path(arg)
                log_func(f"[{'green' if ok else 'red'}]{msg}[/]")
            
            elif root == "/kbclear":
                msgs = clear_kb(all_data=(arg == "--all"))
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

            elif root == "/pdf2md":
                from src.skills.pdf_tools import convert_pdf_path
                outs, errs = convert_pdf_path(arg, recursive=True)
                log_func(f"PDF Convert: Done={len(outs)}, Errors={len(errs)}")
            
            elif root == "/docx2md":
                from src.skills.docx_tools import convert_docx_path
                outs, errs = convert_docx_path(arg, recursive=True)
                log_func(f"Docx Convert: Done={len(outs)}, Errors={len(errs)}")
            
            elif root == "/xlsx2md":
                from src.skills.xlsx_tools import convert_xlsx_path
                outs, errs = convert_xlsx_path(arg, recursive=True)
                log_func(f"Excel Convert: Done={len(outs)}, Errors={len(errs)}")
                
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

            elif root == "/undo":
                # 尝试调用 cli 层的 undo
                executable = __import__('sys').executable
                main_script = str(__import__('src.utils.config', fromlist=['PROJECT_ROOT']).PROJECT_ROOT / "src" / "main.py")
                res = __import__('subprocess').run([executable, main_script, "undo"], capture_output=True, text=True, encoding="utf-8")
                log_func(f"[yellow]⏪ 撤销执行结果:[/yellow]\n{res.stdout or res.stderr}")

            elif root == "/md2canvas_ai":
                log_func("[magenta]🚀 AI Canvas 转换引擎已准备就绪，正在分析当前上下文...[/magenta]")

            elif root == "/version":
                log_func("[bold cyan]WikiCoder Pro TUI v3.2.1 [Full Align][/bold cyan]")

            else:
                log_func(f"[red]未知指令: {root}[/red]")
                
        except Exception as e:
            log_func(f"[bold red]指令执行异常:[/bold red] {str(e)}")
