import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

sys.path.append(os.getcwd())
from src.utils.db_manager import search_chunks

def find_precise_norm():
    target = "传输线路代维负责"
    print(f"--- 正在全库精确搜索关键词: {target} ---")
    
    # 直接使用底层 FTS 检索
    rows = search_chunks(target, limit=10)
    
    if not rows:
        print("❌ 警告：全库检索均未发现该目标文本！请确认 Wiki 库中是否真的包含该段落。")
        return

    print(f"找到 {len(rows)} 个相关片段：")
    for i, r in enumerate(rows):
        print(f"\n[{i+1}] 文件: {r['parent_file']}")
        print(f"    标题: {r['title']}")
        content = r['content_text']
        if target in content:
            print("    ✅ 匹配成功！")
            pos = content.find(target)
            print(f"    片段内容: ...{content[max(0, pos-100):pos+100]}...")
        else:
            print(f"    [模糊匹配] 内容节选: {content[:100]}...")

if __name__ == "__main__":
    find_precise_norm()
