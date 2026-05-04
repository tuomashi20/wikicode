from pathlib import Path
from src.utils.config import AppConfig

def _save_memory_markdown(config: AppConfig, title: str, content: str) -> Path:
    """
    将 AI 整理的会话草稿保存到本地知识库的 raw/faq 目录下。
    
    参数:
        config: 全局配置对象
        title: 文档标题（将作为文件名）
        content: Markdown 内容
    返回:
        保存后的 Path 对象
    """
    vault_path = Path(config.wiki_strategy.vault_path)
    raw_dir = config.wiki_strategy.raw_dir
    # 按照约定，存入 raw/faq 目录
    faq_dir = vault_path / raw_dir / "faq"
    faq_dir.mkdir(parents=True, exist_ok=True)
    
    # 简单的文件名清理，防止特殊字符导致保存失败
    safe_title = "".join([c for c in title if c.isalnum() or c in (' ', '-', '_')]).strip()
    if not safe_title:
        safe_title = "session_draft"
        
    file_path = faq_dir / f"{safe_title}.md"
    file_path.write_text(content, encoding="utf-8")
    
    return file_path
