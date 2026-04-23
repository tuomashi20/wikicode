from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console

# 引入核心组件用于 AI 功能
from src.core.llm_client import LLMClient
from src.utils.config import load_config

console = Console()

def split_markdown_by_headings(content: str) -> list[dict[str, Any]]:
    """
    解析 Markdown 内容，按标题层级拆分（正则版限制最高 3 层）。
    Level 1: # 标题
    Level 2: 一、标题
    Level 3: （一）标题
    3层以下内容合并到第3层中。
    """
    lines = content.splitlines()
    sections = []
    current_section = {"level": 0, "title": "Root", "body": []}
    
    # 标题识别正则 (仅识别前三层)
    std_heading_re = re.compile(r"^(#{1,6})\s+(.*)$")
    zh_main_re = re.compile(r"^([一二三四五六七八九十百]+[、.])\s*(.*)$")
    zh_sub_re = re.compile(r"^([\(（][一二三四五六七八九十百]+[\)）])\s*(.*)$")
    
    def safe_strip(val):
        return val.strip() if val else ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current_section["body"]:
                current_section["body"].append("")
            continue
            
        match_std = std_heading_re.match(line)
        match_zh_m = zh_main_re.match(line)
        match_zh_s = zh_sub_re.match(line)
        
        found_heading = False
        level = 0
        title = ""
        
        if match_std:
            level = len(match_std.group(1))
            title = safe_strip(match_std.group(2))
            found_heading = True
        elif match_zh_m and len(line) < 100:
            level = 2
            title = safe_strip(match_zh_m.group(1)) + safe_strip(match_zh_m.group(2))
            found_heading = True
        elif match_zh_s and len(line) < 100:
            level = 3
            title = safe_strip(match_zh_s.group(1)) + safe_strip(match_zh_s.group(2))
            found_heading = True
            
        # 强制限制：如果识别出的层级超过 3 层，我们将其视为正文
        if found_heading and level > 3:
            found_heading = False
            
        if found_heading:
            if current_section["body"] or current_section["level"] > 0:
                sections.append({
                    "level": current_section["level"],
                    "title": current_section["title"],
                    "body": "\n".join(current_section["body"]).strip()
                })
            current_section = {"level": level, "title": title, "body": []}
        else:
            current_section["body"].append(line)
            
    if current_section["body"] or current_section["level"] > 0:
        sections.append({
            "level": current_section["level"],
            "title": current_section["title"],
            "body": "\n".join(current_section["body"]).strip()
        })
    return sections

def split_markdown_by_llm(content: str) -> list[dict[str, Any]]:
    """
    使用 AI 解析 Markdown 的逻辑层级结构（恢复深度细化）。
    """
    config = load_config()
    client = LLMClient(config.llm)
    
    system_prompt = """你是一个专业的文档结构深度分析专家。你的任务是将 Markdown 文档彻底拆解为多层级的树状 Canvas 结构。

核心规则：
1. **极致细化**：严禁过度总结。文档中出现的任何层级标志（如 #, 一、, （一）, 1., 1.1, (1), ①, 甚至明显的加粗小标题）都必须被识别为独立的节点。
2. **严禁包含子章节**：父节点的 "body" 字段只能包含紧随该标题之后、且在下一个子标题出现之前的文字。严禁将带有编号的子章节内容直接放入父节点的 "body" 中。
3. **深度嵌套**：必须识别出所有逻辑嵌套关系（4层、5层甚至更深）。如果 1. 下面有 (1)，那么 (1) 必须是 1. 的 children。
4. **完整性**：保留文档的所有实质性文字内容，不要进行压缩或改写。

输出格式要求为纯 JSON 数组，每个对象结构：
{
  "title": "标题全称",
  "body": "该标题下的纯正文内容",
  "children": [ ...子章节对象 ]
}"""

    user_prompt = f"请分析以下文档结构并输出 JSON：\n\n{content}"
    
    with console.status("[cyan]正在通过 AI 分析文档结构...[/cyan]"):
        try:
            response = client.generate(system_prompt, user_prompt)
            json_match = re.search(r"(\[.*\])", response, re.DOTALL)
            if json_match:
                return json.loads(json_match.group(1))
            else:
                return json.loads(response.strip("`").strip("json").strip())
        except Exception as e:
            console.print(f"[red]AI 解析失败：{e}[/red]")
            return []

def build_canvas_data(sections: list[dict[str, Any]], from_ai: bool = False) -> dict[str, Any]:
    """
    实现平衡树型布局。
    """
    if not sections:
        return {"nodes": [], "edges": []}

    class Node:
        def __init__(self, data):
            self.title = data.get("title", "Untitled")
            self.body = data.get("body", "")
            self.id = uuid.uuid4().hex[:16]
            self.children = []
            self.x = 0
            self.y = 0
            content_len = len(self.title) + len(self.body)
            self.w = min(800, max(450, 450 + (content_len // 100) * 50))
            self.h = min(1000, max(350, 350 + (content_len // 200) * 100))
            self.subtree_width = 0

    def convert_to_nodes_recursive(data_list):
        res = []
        for d in data_list:
            n = Node(d)
            if "children" in d and d["children"]:
                n.children = convert_to_nodes_recursive(d["children"])
            res.append(n)
        return res

    if from_ai:
        root_nodes = convert_to_nodes_recursive(sections)
    else:
        root_nodes = []
        last_nodes_by_level = {}
        for sec in sections:
            new_node = Node(sec)
            level = max(1, sec.get("level", 1))
            if level == 1 or not last_nodes_by_level:
                root_nodes.append(new_node)
            else:
                p_level = level - 1
                while p_level >= 1 and p_level not in last_nodes_by_level:
                    p_level -= 1
                if p_level in last_nodes_by_level:
                    last_nodes_by_level[p_level].children.append(new_node)
                else:
                    root_nodes.append(new_node)
            last_nodes_by_level[level] = new_node

    X_GAP = 150
    Y_GAP = 1200

    def calculate_subtree_width(node):
        if not node.children:
            node.subtree_width = node.w
            return node.w
        c_width = sum(calculate_subtree_width(c) for c in node.children)
        c_width += X_GAP * (len(node.children) - 1)
        node.subtree_width = max(node.w, c_width)
        return node.subtree_width

    for root in root_nodes:
        calculate_subtree_width(root)

    canvas_nodes = []
    canvas_edges = []

    def layout_node(node, start_x, y):
        node.x = start_x + (node.subtree_width - node.w) / 2
        node.y = y
        canvas_nodes.append({
            "id": node.id,
            "type": "text",
            "text": f"# {node.title}\n\n{node.body}" if node.body else f"# {node.title}",
            "x": node.x,
            "y": node.y,
            "width": node.w,
            "height": node.h
        })
        current_x = start_x
        c_total_width = sum(c.subtree_width for c in node.children) + X_GAP * (len(node.children) - 1)
        if node.w > c_total_width:
            current_x += (node.w - c_total_width) / 2
        for child in node.children:
            layout_node(child, current_x, y + Y_GAP)
            canvas_edges.append({
                "id": uuid.uuid4().hex[:16],
                "fromNode": node.id,
                "toNode": child.id
            })
            current_x += child.subtree_width + X_GAP

    total_x = 0
    for root in root_nodes:
        layout_node(root, total_x, 0)
        total_x += root.subtree_width + X_GAP * 2

    return {"nodes": canvas_nodes, "edges": canvas_edges}

def convert_md_file_to_canvas(md_path: Path, use_ai: bool = False) -> Path:
    content = md_path.read_text(encoding="utf-8", errors="ignore")
    if use_ai:
        sections = split_markdown_by_llm(content)
        canvas_data = build_canvas_data(sections, from_ai=True)
    else:
        sections = split_markdown_by_headings(content)
        canvas_data = build_canvas_data(sections, from_ai=False)
    
    out_path = md_path.with_suffix(".canvas")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(canvas_data, f, ensure_ascii=False, indent=2)
    return out_path

def convert_md_canvas_path(path_str: str, recursive: bool = False, use_ai: bool = False) -> tuple[list[Path], list[str]]:
    p = Path(path_str).expanduser()
    if not p.is_absolute(): p = (Path.cwd() / p).resolve()
    if not p.exists(): return [], [f"路径不存在：{p}"]

    files = [p] if p.is_file() else list(p.rglob("*.md") if recursive else p.glob("*.md"))
    files = [f for f in files if f.is_file() and f.suffix.lower() == ".md"]
    if not files: return [], [f"未找到 .md 文件"]

    outs, errs = [], []
    for f in sorted(files):
        try:
            outs.append(convert_md_file_to_canvas(f, use_ai=use_ai))
        except Exception as e:
            errs.append(f"转换失败：{f} | {e}")
    return outs, errs

def handle_canvas_command(cmd_text: str) -> None:
    is_ai = cmd_text.startswith("/md2canvas_ai")
    cmd_name = "/md2canvas_ai" if is_ai else "/md2canvas"
    
    if cmd_text.strip() == cmd_name:
        console.print(f"[yellow]用法：{cmd_name} <路径> [-r][/yellow]")
        return
        
    arg = cmd_text.split(" ", 1)[1].strip()
    recursive = False
    if " -r" in arg or " --recursive" in arg:
        recursive = True
        arg = arg.replace(" --recursive", "").replace(" -r", "").strip()
        
    outs, errs = convert_md_canvas_path(arg, recursive=recursive, use_ai=is_ai)
    
    for o in outs: console.print(f"[green]已生成：{o}[/green]")
    for e in errs: console.print(f"[yellow]{e}[/yellow]")
    if outs and not errs: console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")
