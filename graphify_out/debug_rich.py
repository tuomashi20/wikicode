import json
from pathlib import Path

p = Path(r"d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
data = json.loads(p.read_text(encoding='utf-8'))
atoms = [n for n in data['nodes'] if n.get('type') == 'semantic_atom']
rich_atoms = [a for a in atoms if len(a.get('properties', {})) > 0]

print(f"Total: {len(atoms)} | Rich: {len(rich_atoms)}")
for a in rich_atoms[:5]:
    print(f"\n[Label]: {a.get('label')}")
    print(f"[Content]: {a.get('content')}")
    print(f"[Properties]: {json.dumps(a.get('properties'), ensure_ascii=False)}")
