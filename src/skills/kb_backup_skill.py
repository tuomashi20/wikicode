"""
kb_backup_skill.py - 知识库备份与恢复 Skill。
"""
from typing import List, Dict, Any, Tuple
from src.utils.config import load_config, ensure_workspace
from src.utils.kb_backup import list_kb_backups, restore_kb_backup, save_kb_backup

def get_backups(limit: int = 30) -> List[Dict[str, Any]]:
    """获取知识库备份列表"""
    ensure_workspace()
    return list_kb_backups(limit=limit)

def create_backup(name: str = None) -> Tuple[str, List[str]]:
    """创建当前知识库备份"""
    ensure_workspace()
    cfg = load_config()
    return save_kb_backup(cfg, name=name)

def restore_backup_by_id(backup_id: str) -> Tuple[bool, List[str]]:
    """从备份 ID 恢复知识库"""
    ensure_workspace()
    cfg = load_config()
    return restore_kb_backup(cfg, backup_id)
