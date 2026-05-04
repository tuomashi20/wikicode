"""
chat_archive_skill.py - 聊天记录归档 Skill。
将对话历史整理为规范的 Markdown 文档并保存。
"""
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple
from src.utils.config import load_config, ensure_workspace

def mem_draft_archive(history: List[Dict[str, str]], filename: str = None) -> Tuple[bool, str]:
    """整理会话草稿 (/memdraft)"""
    return archive_chat_to_md(history, filename, sub_dir="drafts", prefix="draft_")

def mem_save_archive(history: List[Dict[str, str]], filename: str = None) -> Tuple[bool, str]:
    """保存对话精华到 Wiki 记忆库 (/memsave)"""
    return archive_chat_to_md(history, filename, sub_dir="wiki/memories", prefix="memory_")

def archive_chat_to_md(history: List[Dict[str, str]], filename: str = None, sub_dir: str = "archives", prefix: str = "chat_") -> Tuple[bool, str]:
    """
    将聊天历史保存为 Markdown 文件。
    """
    try:
        ensure_workspace()
        cfg = load_config()
        
        # 确定保存目录
        archive_dir = Path(cfg.wiki_strategy.raw_path) / sub_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        
        if not filename:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"{prefix}{timestamp}.md"
        
        if not filename.endswith(".md"):
            filename += ".md"
            
        file_path = archive_dir / filename
        
        title_map = {
            "archives": "WikiCoder 全量对话归档",
            "drafts": "WikiCoder 会话草稿 (临时)",
            "wiki/memories": "WikiCoder 核心知识记忆"
        }
        title = title_map.get(sub_dir, "WikiCoder 对话存档")
        
        content = [f"# {title}\n生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"]
        
        aligned_history = []
        for item in history:
            q = item.get("q", item.get("question", "")).strip()
            a = item.get("a", item.get("answer", "")).strip()
            if q or a:
                aligned_history.append({"q": q or "[无提问]", "a": a or "[未回答]"})

        # --- 内容差异化处理逻辑 ---
        if sub_dir == "wiki/memories":
            # [核心记忆]：提取所有代码块和核心结论
            content.append("> [!NOTE]\n> 本文档由 /memsave 自动提取，仅保留对话中的核心技术内容与结论。\n")
            all_code_blocks = []
            import re
            for item in aligned_history:
                # 使用正则提取 Markdown 代码块
                code_blocks = re.findall(r"```[\s\S]*?```", item['a'])
                all_code_blocks.extend(code_blocks)
            
            if all_code_blocks:
                content.append("## 🛠️ 核心代码与配置\n")
                content.extend(all_code_blocks)
            else:
                # 如果没代码块，就存最后一段最有价值的回答
                content.append("## 💡 核心结论\n")
                content.append(aligned_history[-1]['a'] if aligned_history else "无内容")

        elif sub_dir == "drafts":
            # [会话草稿]：仅保留最新的 1-2 轮对话，侧重于当前任务
            content.append("> [!TIP]\n> 本文档由 /memdraft 生成，仅保留最近的会话脉络。\n")
            recent = aligned_history[-2:] if len(aligned_history) >= 2 else aligned_history
            for i, item in enumerate(recent, 1):
                content.append(f"### 🎯 当前焦点 {i}: {item['q']}")
                content.append(f"{item['a']}\n")
        
        else:
            # [全量归档]：保持完整历史
            for i, item in enumerate(aligned_history, 1):
                content.append(f"### Q{i}: {item['q']}")
                content.append(f"{item['a']}\n")
                content.append("---")
            
        file_path.write_text("\n".join(content), encoding="utf-8")
        
        return True, str(file_path)
    except Exception as e:
        return False, str(e)
