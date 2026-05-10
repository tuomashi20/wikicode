"""
wiki_skill.py - 知识库核心操作 Skill。
实现了同步、清空、结构查询与路径设置等原子功能。
"""
from pathlib import Path
from typing import Any, Dict, List

from src.utils.config import AppConfig, load_config, ensure_workspace
from src.core.atomizer import Atomizer
from src.utils.db_manager import clear_index_store
from src.skills.wiki_tools import wiki_list_structure

def sync_kb() -> Dict[str, Any]:
    """[单一业务入口] 执行知识库全量/增量同步，并联动 Graphify 图谱"""
    ensure_workspace()
    config = load_config()
    atomizer = Atomizer(config)
    result = atomizer.sync()
    
    # [联动：Graphify 知识图谱增量同步]
    try:
        import sys
        import os
        project_root = os.getcwd()
        if project_root not in sys.path:
            sys.path.append(project_root)
            
        from graphify_out.sync_gateway import run_incremental_graph
        run_incremental_graph(config)
    except Exception as e:
        # 记录警告但不阻塞主流程
        print(f"\n[Graphify Warning] 知识图谱同步跳过: {str(e)}")
        
    return result

def _clear_gbrain_remotely(slugs: List[str]) -> List[str]:
    """[V3.0] 物理注销 gbrain 中的远程镜像"""
    from src.core.mcp_client import GBrainMCPClient
    results = []
    try:
        client = GBrainMCPClient()
        for slug in slugs:
            # 直接调用，底层已具备重试机制
            res = client.call_tool("delete_page", {"slug": slug})
            results.append(f"Remote: gbrain clear '{slug}' -> DONE")
    except Exception as e:
        results.append(f"Remote: gbrain clear failed -> FAIL: {str(e)}")
    return results

def clear_kb(all_data: bool = False) -> List[str]:
    """[V3.0 交互终极版] 清空向量索引存储"""
    ensure_workspace()
    cfg = load_config()
    final_msgs = []
    
    final_msgs.append("START: 正在启动知识库全量清理程序...")
    
    # 1. 本地清理
    from src.utils.db_manager import clear_index_store
    local_raw_msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
    final_msgs.extend([m for m in local_raw_msgs if "已完成" not in m])
    
    # 2. 深度清理
    if all_data:
        from src.cli.repl import _clear_wiki_output
        final_msgs.extend(_clear_wiki_output(cfg.wiki_strategy.wiki_path))
        
        final_msgs.append("START: 正在同步注销 gbrain 远程镜像 (请稍候)...")
        remote_tasks = ["wiki/raw", "wiki", "personal_profile"]
        remote_results = _clear_gbrain_remotely(remote_tasks)
        final_msgs.extend(remote_results)
        final_msgs.append("DONE: 知识库全量清理（本地+远程镜像）已彻底完成。")
    else:
        final_msgs.append("DONE: 知识库本地索引已清理完毕。")
    
    return final_msgs

def get_structure() -> List[Dict[str, Any]]:
    """[单一业务入口] 获取知识库索引结构"""
    ensure_workspace()
    return wiki_list_structure()

def set_vault_path(path: str) -> tuple[bool, str]:
    """[单一业务入口] 设置知识库根目录并重构工作空间"""
    try:
        import yaml
        from src.utils.config import DEFAULT_CONFIG_PATH
        
        path_obj = Path(path).absolute()
        config_path = DEFAULT_CONFIG_PATH
        
        # 1. 更新配置文件 (SSOT: Single Source of Truth)
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        if not isinstance(data, dict): data = {}
        ws = data.setdefault("wiki_strategy", {})
        ws["vault_path"] = str(path_obj)
        
        # 补全标准子目录定义
        ws.setdefault("raw_dir", "raw")
        ws.setdefault("wiki_dir", "wiki")
        ws.setdefault("processed_dir", "wiki_processed")
        
        config_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        
        # 2. 核心联动：立即重构新路径下的工作空间
        new_config = load_config()
        ensure_workspace(new_config)
        
        return True, f"OK: Vault path switched to {path_obj}. Please run 'sync' to initialize."
    except Exception as e:
        return False, f"ERROR: Path setting failed: {str(e)}"
