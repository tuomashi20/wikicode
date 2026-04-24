import sys
from pathlib import Path
import json

# 添加 src 到路径
sys.path.append(str(Path.cwd()))

from src.skills.canvas_tools import convert_md_file_to_canvas

test_file = Path("scratch/test_zh_headings.md")
try:
    out_path = convert_md_file_to_canvas(test_file)
    print(f"成功生成: {out_path}")
    data = json.loads(out_path.read_text(encoding="utf-8"))
    print(f"节点数量: {len(data['nodes'])}")
    print(f"连线数量: {len(data['edges'])}")
    for node in data['nodes']:
        print(f"Node ID: {node['id']}, Level X: {node['x']}, Width: {node['width']}, Title: {node['text'].splitlines()[0]}")
except Exception as e:
    print(f"执行失败: {e}")
    import traceback
    traceback.print_exc()
