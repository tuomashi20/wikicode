import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

class WikiGenerator:
    """原子模块 2：负责 Module-Matrix 风格的页面生成与排版"""

    def __init__(self, wiki_dir: Path):
        self.wiki_dir = wiki_dir

    def render_page(self, category: str, name: str, data: Dict[str, Any]) -> Path:
        """根据模板渲染单页 Wiki"""
        target_path = self.wiki_dir / category / f"{self._safe_name(name)}.md"
        target_path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f"# {name}",
            "",
            f"> [!INFO] 分类: {category} | 生成时间: {self._now()}",
            ""
        ]

        # 1. 核心定义模块
        if data.get("definitions"):
            lines.append("## 📌 核心定义 (Definitions)")
            for d in data["definitions"]:
                d_name = d.get("name") or d.get("entity") or "Unknown"
                d_summary = d.get("summary") or d.get("context") or d.get("content") or ""
                lines.append(f"- **{d_name}**: {d_summary} {self._source_link(d)}")
            lines.append("")

        # 2. 职责矩阵模块 (WikiCoder 3.0 表格化)
        if data.get("responsibilities"):
            lines.append("## 🛡️ 职责矩阵 (Responsibility Matrix)")
            lines.append("| 主体 | 动作 | 客体 | 边界/条件 | 溯源 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- |")
            for r in data["responsibilities"]:
                subj = r.get("subject") or "未知"
                act = r.get("action") or "维护"
                obj = r.get("object") or name
                cond = r.get("condition") or "通用"
                link = self._source_link(r)
                lines.append(f"| {subj} | {act} | {obj} | {cond} | {link} |")
            lines.append("")

        # 3. 维护界面模块
        interfaces = [f for f in data.get("raw_facts", []) if f.get("type") == "interfaces"]
        if interfaces:
            lines.append("## 🚧 维护界面 (Interface Boundaries)")
            for i in interfaces:
                i_summary = i.get("summary") or i.get("content") or ""
                i_cond = i.get("condition", "")
                suffix = f" (条件: {i_cond})" if i_cond else ""
                lines.append(f"- {i_summary}{suffix} {self._source_link(i)}")
            lines.append("")

        # 4. 冲突与推理标记
        if data.get("inferences"):
            lines.append("## 🧠 逻辑推理与关联 (AI Inferences)")
            for i in data["inferences"]:
                i_content = i.get("content") or i.get("summary") or ""
                lines.append(f"> [!WARNING] [AI 推理]\n> {i_content} {self._source_link(i)}")
            lines.append("")

        # 4. 溯源链接区域
        lines.append("---")
        lines.append("*由 WikiCoder 3.0 编译器生成*")

        target_path.write_text("\n".join(lines), encoding="utf-8")
        return target_path

    def _safe_name(self, name: str) -> str:
        return re.sub(r"[\\/:*?\"<>|]+", "_", name)

    def _source_link(self, item: Dict[str, Any]) -> str:
        """生成指向 Raw 文档的锚点链接"""
        source = item.get("source", "Unknown")
        anchor = item.get("anchor", "")
        return f"[[{source}#{anchor}]]" if anchor else f"[[{source}]]"

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")
