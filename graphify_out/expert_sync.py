import json
import re
import os
import hashlib
import sys
import yaml
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

# 强制加入项目路径以引用 LLMClient
sys.path.append(r"d:/project/wikicode")
from src.core.llm_client import LLMClient

CACHE_DIR = Path(r"d:/project/wikicode/graphify_out/semantic_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def get_content_hash(content):
    return hashlib.md5(content.encode('utf-8')).hexdigest()

def extract_atomic_semantics(content: str, filename: str, llm_client: LLMClient):
    content_hash = get_content_hash(content)
    cache_file = CACHE_DIR / f"{content_hash}.json"
    
    # 如果已有缓存，直接跳过 (确保不再重复消耗额度)
    if cache_file.exists():
        try: return json.loads(cache_file.read_text(encoding='utf-8'))
        except: pass

    # 深度语义 Prompt
    sys_prompt = """你是一个高级业务流程审计专家。你的任务是将文档拆解为'原子语义节点'。
每个节点必须包含：
1. label: 核心逻辑名称(如:结算比例)
2. content: 原始规则描述
3. properties: 这是一个JSON对象，必须提取出文中的[数值、百分比、金额、判定条件、期限]等硬性指标。
请务必返回一个纯JSON数组格式。"""
    
    user_prompt = f"文件: {filename}\n内容: {content[:2000]}\n请提取原子逻辑:"
    
    try:
        response = llm_client.generate(sys_prompt, user_prompt)
        matches = re.search(r'\[.*\]', response, re.DOTALL)
        if matches:
            atoms_raw = json.loads(matches.group())
            atoms = []
            for a in atoms_raw:
                atom_id = f"atom_{filename}_{hash(a.get('label',''))}"
                atoms.append({
                    "id": atom_id,
                    "label": a.get("label", "Unknown"),
                    "type": "semantic_atom",
                    "content": a.get("content", ""),
                    "properties": a.get("properties", {}),
                    "parent_file": filename,
                    "keywords": re.findall(r'[\u4e00-\u9fa5]{2,}', a.get("content", ""))
                })
            cache_file.write_text(json.dumps(atoms, ensure_ascii=False), encoding='utf-8')
            return atoms
    except Exception as e:
        print(f"DEBUG: AI Failed for {filename}: {e}")
    return [] # 如果 AI 挂了，这一次我们宁愿不存，也不要垃圾数据

def build_expert_graph():
    # 初始化
    config_path = Path("d:/project/wikicode/.wikicoder/config.yaml")
    with open(config_path, 'r', encoding='utf-8') as f:
        config_raw = yaml.safe_load(f)
    
    class MockConfig:
        def __init__(self, d): self.__dict__.update(d)
    
    llm = LLMClient(MockConfig(config_raw['llm']))
    
    raw_dir = Path(r"D:/lihq_obsi/lihq_obsi/LLM_wiki/raw")
    output_json = Path(r"d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
    
    file_list = [f for f in raw_dir.glob("**/*.md")]
    print(f"--- [EXPERT SYNC] Total Files: {len(file_list)} ---")

    nodes = []
    edges = []
    
    def worker(f_path):
        f_name = f_path.name
        f_id = f"file_{f_name}"
        local_nodes = [{"id": f_id, "label": f_name, "type": "file"}]
        local_edges = []
        try:
            content = f_path.read_text(encoding='utf-8', errors='ignore')
            atoms = extract_atomic_semantics(content, f_name, llm)
            for a in atoms:
                local_nodes.append(a)
                local_edges.append({"from": f_id, "to": a["id"], "label": "defines"})
            print(f"DONE: {f_name} (Atoms: {len(atoms)})")
            return local_nodes, local_edges
        except: return [], []

    # 采用极稳健的 2 路并发
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(worker, file_list))

    for ln, le in results:
        nodes.extend(ln); edges.extend(le)

    # 保存最终成果
    result = {"nodes": nodes, "edges": edges}
    output_json.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    print(f"--- [FINISH] Expert Graph Built. Total Nodes: {len(nodes)} ---")

if __name__ == "__main__":
    build_expert_graph()
