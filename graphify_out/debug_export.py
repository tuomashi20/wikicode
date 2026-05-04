import json
from pathlib import Path

def export_sample():
    p = Path(r"d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
    if not p.exists():
        print("JSON not found")
        return
    
    data = json.loads(p.read_text(encoding='utf-8'))
    atoms = [n for n in data['nodes'] if n.get('type') == 'semantic_atom']
    
    output = []
    output.append("# 业务逻辑原子语义抽样 (前 20 条)")
    output.append(f"\n系统共提炼出 **{len(atoms)}** 个语义定义。\n")
    
    for a in atoms[:20]:
        label = a.get("label", "未命名")
        content = a.get("content", "无内容")
        props = a.get("properties", {})
        source = a.get("parent_file", "未知")
        
        output.append(f"### {label}")
        output.append(f"- **来源**: {source}")
        output.append(f"- **语义定义**: {content}")
        if props:
            output.append(f"- **结构化属性**: `{json.dumps(props, ensure_ascii=False)}`")
        output.append("\n---")
    
    Path(r"d:/project/wikicode/graphify_out/semantic_sample.md").write_text("\n".join(output), encoding='utf-8')
    print("SUCCESS: semantic_sample.md has been generated.")

if __name__ == "__main__":
    export_sample()
