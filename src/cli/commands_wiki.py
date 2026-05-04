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

def run_sync() -> dict[str, int]:
    config = load_config()
    atomizer = Atomizer(config)
    result = atomizer.sync()
    
    # [集成：Graphify 知识图谱增量同步]
    try:
        # 动态导入，避免循环依赖或安装环境不全导致主流程崩溃
        import sys
        import os
        project_root = os.getcwd()
        if project_root not in sys.path:
            sys.path.append(project_root)
            
        from graphify_out.sync_gateway import run_incremental_graph
        run_incremental_graph(config)
    except Exception as e:
        # 仅打印错误，不阻塞主同步流程
        print(f"\n[Graphify Integration Warning] 知识图谱同步跳过: {str(e)}")
        
    return result

@app.command()
def sync() -> None:
    """执行知识库同步（增量）。"""
    ensure_workspace()
    result = run_sync()
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
    all_data: bool = typer.Option(False, "--all", help="同时清空 wiki 页面"),
) -> None:
    """清空向量索引存储。"""
    if not confirm:
        console.print("[yellow]危险操作，请使用 --yes 确认。[/yellow]")
        return
    ensure_workspace()
    cfg = load_config()
    msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
    if all_data:
        from src.cli.repl import _clear_wiki_output
        msgs.extend(_clear_wiki_output(cfg.wiki_strategy.wiki_path))
    for m in msgs:
        console.print(f"[green]{m}[/green]" if m.startswith(("Cleared", "Removed", "Truncated")) else f"[yellow]{m}[/yellow]")
    console.print("[cyan]操作完成。可执行 sync 重新构建。[/cyan]")

@app.command()
def kbbackups() -> None:
    """查看知识库备份列表。"""
    ensure_workspace()
    items = list_kb_backups(limit=30)
    if not items:
        console.print("No KB backups found.")
    else:
        for it in items:
            console.print(f"- {it['id']} | {it['created_at']}")

@app.command()
def kbsave(name: str = typer.Option("", help="备份名称")) -> None:
    """备份当前知识库状态。"""
    ensure_workspace()
    cfg = load_config()
    bid, msgs = save_kb_backup(cfg, name=name or None)
    console.print(f"[green]KB backup created:[/green] {bid}")
    for m in msgs:
        console.print(f"[yellow]{m}[/yellow]")

@app.command()
def kbrestore(backup_id: str) -> None:
    """从备份 ID 恢复知识库。"""
    ensure_workspace()
    cfg = load_config()
    ok, msgs = restore_kb_backup(cfg, backup_id)
    for m in msgs:
        console.print(f"[green]{m}[/green]" if m.startswith("Restored") else f"[yellow]{m}[/yellow]")
    if ok:
        console.print("[cyan]KB restore completed.[/cyan]")

@app.command()
def structure() -> None:
    """查看当前知识库索引结构。"""
    ensure_workspace()
    items = wiki_list_structure()
    if not items:
        console.print("No indexed wiki chunks.")
    else:
        for item in items:
            console.print(f"- {item['parent_file']} ({item['chunk_count']} chunks)")

@app.command()
def vaultpath(path: str) -> None:
    """设置知识库根目录。"""
    ok, msg = _set_vault_path(path)
    if ok:
        config = load_config()
        ensure_workspace(config)
        console.print(f"[green]{msg}[/green]")
    else:
        console.print(f"[red]{msg}[/red]")

def _set_vault_path(path_str: str) -> tuple[bool, str]:
    path_str = path_str.strip()
    if not path_str:
        return False, "路径不能为空。"
    cfg_path = DEFAULT_CONFIG_PATH
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict): data = {}
    ws = data.get("wiki_strategy") or {}
    if not isinstance(ws, dict): ws = {}
    ws["vault_path"] = path_str
    ws.setdefault("raw_dir", "raw")
    ws.setdefault("wiki_dir", "wiki")
    ws.setdefault("processed_dir", "wiki_processed")
    data["wiki_strategy"] = ws
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return True, f"已更新 vault_path 为: {path_str}"
