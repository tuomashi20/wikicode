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
    """执行知识库增量同步"""
    ensure_workspace()
    config = load_config()
    atomizer = Atomizer(config)
    return atomizer.sync()

def clear_kb(all_data: bool = False) -> List[str]:
    """清空向量索引存储"""
    ensure_workspace()
    cfg = load_config()
    msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
    
    if all_data:
        # 如果需要清理物理生成的 wiki 页面，这里调用相关逻辑
        from src.cli.repl import _clear_wiki_output
        msgs.extend(_clear_wiki_output(cfg.wiki_strategy.wiki_path))
    return msgs

def get_structure() -> List[Dict[str, Any]]:
    """获取知识库索引结构"""
    ensure_workspace()
    return wiki_list_structure()

def set_vault_path(path: str) -> tuple[bool, str]:
    """设置知识库根目录"""
    # 逻辑来源于 commands_wiki 内部私有函数
    try:
        from src.utils.config import DEFAULT_CONFIG_PATH
        config_path = DEFAULT_CONFIG_PATH
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                import yaml
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        
        data.setdefault("wiki_strategy", {})["raw_path"] = str(Path(path).absolute())
        
        with open(config_path, "w", encoding="utf-8") as f:
            import yaml
            yaml.dump(data, f, allow_unicode=True)
            
        return True, f"Vault path updated to: {path}"
    except Exception as e:
        return False, str(e)
