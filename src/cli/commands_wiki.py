import typer
from pathlib import Path
import yaml
from src.cli.base import app, console
from src.cli.display import _print_runtime_settings
from src.utils.config import AppConfig, DEFAULT_CONFIG_PATH, load_config, ensure_workspace
from src.core.atomizer import Atomizer
from src.utils.db_manager import clear_index_store
from src.utils.kb_backup import list_kb_backups, restore_kb_backup, save_kb_backup
from src.skills.wiki_tools import wiki_list_structure

@app.command()
def sync() -> None:
    """执行知识库同步（增量）。"""
    ensure_workspace()
    from src.skills.wiki_skill import sync_kb
    result = sync_kb()
    
    wp = result.get("wiki_pages", 0)
    sk = result.get("skipped", 0)
    dl = result.get("deleted", 0)
    console.print(
        f"[green]Sync completed[/green]: changed={result['files']} skipped={sk} deleted={dl} "
        f"chunks={result['chunks']} wiki_pages={wp}"
    )
@app.command()
def kbclear(
    confirm: bool = typer.Option(False, "--yes", help="确认清空索引"),
    all_data: bool = typer.Option(False, "--all", help="同时清空 wiki 页面和 gbrain 镜像"),
) -> None:
    """[单一逻辑入口] 清空所有知识库（本地索引 + 远程镜像）。"""
    if not confirm:
        console.print("[yellow]WARNING: This is a destructive operation. Use --yes to confirm.[/yellow]")
        return
    ensure_workspace()
    
    # 核心：直接调用 Skill 层的标准功能，杜绝多处维护逻辑
    from src.skills.wiki_skill import clear_kb
    msgs = clear_kb(all_data=all_data)
    
    for m in msgs:
        console.print(f"[green]{m}[/green]" if "OK" in m or "Cleared" in m or "Remote" in m else f"[yellow]{m}[/yellow]")
    
    console.print("[cyan]Cleanup completed. Source of truth is now reset.[/cyan]")

@app.command()
def kbbackups() -> None:
    """[单一逻辑入口] 查看知识库备份列表。"""
    from src.skills.kb_backup_skill import get_backups
    items = get_backups(limit=30)
    if not items:
        console.print("No KB backups found.")
    else:
        for it in items:
            console.print(f"- {it['id']} | {it['created_at']}")

@app.command()
def kbsave(name: str = typer.Option("", help="备份名称")) -> None:
    """[单一逻辑入口] 备份当前知识库状态。"""
    from src.skills.kb_backup_skill import create_backup
    bid, msgs = create_backup(name=name or None)
    if bid:
        console.print(f"[green]KB backup created:[/green] {bid}")
    for m in msgs:
        console.print(f"[yellow]{m}[/yellow]")

@app.command()
def kbrestore(backup_id: str) -> None:
    """[单一逻辑入口] 从备份 ID 恢复知识库。"""
    from src.skills.kb_backup_skill import restore_backup_by_id
    ok, msgs = restore_backup_by_id(backup_id)
    for m in msgs:
        console.print(f"[green]{m}[/green]" if "Restored" in m or "OK" in m else f"[yellow]{m}[/yellow]")
    if ok:
        console.print("[cyan]KB restore completed.[/cyan]")

@app.command()
def structure() -> None:
    """[单一逻辑入口] 查看当前知识库索引结构。"""
    from src.skills.wiki_skill import get_structure
    items = get_structure()
    if not items:
        console.print("No indexed wiki chunks.")
    else:
        for item in items:
            console.print(f"- {item['parent_file']} ({item['chunk_count']} chunks)")

@app.command()
def vaultpath(path: str) -> None:
    """[单一逻辑入口] 设置知识库根目录。"""
    from src.skills.wiki_skill import set_vault_path
    ok, msg = set_vault_path(path)
    if ok:
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")
