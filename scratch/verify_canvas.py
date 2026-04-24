import sys
from pathlib import Path

# 添加 src 到路径
sys.path.append(str(Path.cwd()))

from src.skills.canvas_tools import convert_md_file_to_canvas

test_file = Path("scratch/test_canvas.md")
try:
    out_path = convert_md_file_to_canvas(test_file)
    print(f"成功生成: {out_path}")
    print("Canvas 内容预览:")
    print(out_path.read_text(encoding="utf-8"))
except Exception as e:
    print(f"执行失败: {e}")
    import traceback
    traceback.print_exc()
