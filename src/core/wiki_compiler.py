from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path

from src.utils.config import AppConfig
from src.utils.db_manager import get_conn


def _safe_name(text: str) -> str:
    s = text.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "untitled"


def _parent_stem(parent_file: str) -> str:
    return _safe_name(Path(parent_file).stem)


def _wikilink(path_no_ext: str) -> str:
    return f"[[{path_no_ext}]]"


class WikiCompiler:
    """Compile Obsidian-friendly wiki pages from chunk index."""

    ENTITY_HINTS = (
        "公司",
        "集团",
        "部门",
        "中心",
        "岗位",
        "角色",
        "供应商",
        "客户",
        "系统",
        "平台",
        "终端",
        "设备",
    )
    CONCEPT_HINTS = (
        "定义",
        "术语",
        "规则",
        "规范",
        "制度",
        "标准",
        "流程",
        "要求",
        "办法",
        "职责",
        "原则",
        "管理",
        "机制",
    )
    COMPARISON_HINTS = ("对比", "比较", "差异", "区别", "优缺点", "选型", "vs")

    TAG_STOPWORDS = {
        "相关",
        "以及",
        "如何",
        "进行",
        "为了",
        "可以",
        "需要",
        "管理",
        "规范",
        "要求",
        "流程",
        "标准",
        "办法",
        "说明",
        "附件",
        "目录",
        "内容",
        "部分",
        "有关",
        "根据",
        "通过",
        "关于",
        "and",
        "the",
        "for",
        "with",
        "from",
        "that",
        "this",
        "what",
        "how",
        "when",
        "where",
    }

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.wiki_root = config.wiki_strategy.wiki_path
        self.entries_dir = self.wiki_root / "entries"
        self.files_dir = self.wiki_root / "files"
        self.tags_dir = self.wiki_root / "tags"

        configured = [str(x).strip() for x in config.wiki_strategy.wiki_subdirs if str(x).strip()]
        self.category_dirs = sorted({_safe_name(x) for x in configured} | {"entries", "queries"})

        self.raw_to_wiki_map = {
            _safe_name(str(k)): _safe_name(str(v))
            for k, v in dict(config.wiki_strategy.raw_to_wiki_map).items()
            if str(k).strip() and str(v).strip()
        }

    def compile(self) -> dict[str, int]:
        self._prepare_dirs()

        with get_conn() as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, title, parent_file, tags, content_text
                FROM chunks
                ORDER BY parent_file, title
                """
            ).fetchall()

        chunks = [dict(r) for r in rows]
        if not chunks:
            self._write_index([], {})
            return {"pages": 0, "files": 0, "tags": 0}

        by_parent: dict[str, list[dict]] = defaultdict(list)
        by_tag: dict[str, list[dict]] = defaultdict(list)
        by_category: dict[str, list[dict]] = defaultdict(list)

        seen_page_names: set[str] = set()
        for c in chunks:
            parent_file = str(c.get("parent_file", ""))
            category = self._category_for_chunk(c)
            parent_stem = _parent_stem(parent_file)

            base_name = _safe_name(f"{parent_stem}__{c.get('title', 'section')}")
            page_name = base_name
            if page_name in seen_page_names:
                page_name = f"{base_name}__{str(c.get('chunk_id', 'x'))[:8]}"
            seen_page_names.add(page_name)

            c["_category"] = category
            c["_entry_rel"] = f"entries/{page_name}"
            c["_page_rel"] = f"{category}/{page_name}"

            by_parent[parent_file].append(c)
            by_category[category].append(c)

            tags = [t.strip() for t in str(c.get("tags", "")).split(",") if t.strip()]
            for t in tags[:12]:
                by_tag[t].append(c)

        for parent_file, group in by_parent.items():
            for c in group:
                related = [x for x in group if x["chunk_id"] != c["chunk_id"]][:8]
                self._write_entry_page(c, parent_file=parent_file, related=related)
                self._write_category_page(c)

        for parent_file, group in by_parent.items():
            self._write_file_page(parent_file, group)

        written_tags = 0
        for tag, group in by_tag.items():
            if not self._is_meaningful_tag(tag):
                continue
            if len({str(x["chunk_id"]) for x in group}) < 2:
                continue
            self._write_tag_page(tag, group)
            written_tags += 1

        self._write_category_indexes(by_category)
        self._write_index(sorted(by_parent.keys()), by_tag)
        return {"pages": len(chunks), "files": len(by_parent), "tags": written_tags}

    def _prepare_dirs(self) -> None:
        self.wiki_root.mkdir(parents=True, exist_ok=True)

        for d in [self.entries_dir, self.files_dir, self.tags_dir]:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

        for cat in self.category_dirs:
            d = self.wiki_root / cat
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)

    def _raw_top_folder(self, parent_file: str) -> str:
        p = Path(parent_file)
        return _safe_name(p.parts[0]) if p.parts else ""

    def _category_for_chunk(self, chunk: dict) -> str:
        parent_file = str(chunk.get("parent_file", ""))
        first = self._raw_top_folder(parent_file)

        mapped = self.raw_to_wiki_map.get(first, "")
        if mapped:
            return mapped

        title = str(chunk.get("title", ""))
        tags = str(chunk.get("tags", ""))
        content = str(chunk.get("content_text", ""))[:500]
        text = f"{title} {tags} {content}".lower()

        if any(k.lower() in text for k in self.COMPARISON_HINTS):
            return "comparisons" if "comparisons" in self.category_dirs else "queries"
        if any(k.lower() in text for k in self.ENTITY_HINTS):
            return "entities" if "entities" in self.category_dirs else "entries"
        if any(k.lower() in text for k in self.CONCEPT_HINTS):
            return "concepts" if "concepts" in self.category_dirs else "entries"
        if "?" in text or "？" in text or "什么" in text or "如何" in text:
            return "queries"

        if "concepts" in self.category_dirs:
            return "concepts"
        return "entries"

    def _write_entry_page(self, chunk: dict, parent_file: str, related: list[dict]) -> None:
        page_rel = str(chunk["_entry_rel"])
        out = self.wiki_root / f"{page_rel}.md"
        out.parent.mkdir(parents=True, exist_ok=True)

        p_stem = _parent_stem(parent_file)
        source_link = _wikilink(f"raw/{parent_file}".replace("\\", "/"))
        file_page = _wikilink(f"files/{p_stem}")

        related_links = ", ".join(_wikilink(str(x["_entry_rel"])) for x in related) if related else "无"

        tags = [t.strip() for t in str(chunk.get("tags", "")).split(",") if t.strip()]
        tag_links = (
            ", ".join(_wikilink(f"tags/{_safe_name(t)}") for t in tags[:10] if self._is_meaningful_tag(t))
            if tags
            else "无"
        )

        text = (
            f"# {chunk.get('title', '未命名章节')}\n\n"
            f"- 来源文件：{source_link}\n"
            f"- 分类：{chunk.get('_category', 'entries')}\n"
            f"- 文件导航：{file_page}\n"
            f"- 标签：{tag_links}\n"
            f"- 相关章节：{related_links}\n\n"
            f"---\n\n"
            f"{chunk.get('content_text', '')}\n"
        )
        out.write_text(text, encoding="utf-8")

    def _write_category_page(self, chunk: dict) -> None:
        out = self.wiki_root / f"{chunk['_page_rel']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)

        source = _wikilink(f"raw/{chunk.get('parent_file', '')}".replace("\\", "/"))
        entry = _wikilink(str(chunk["_entry_rel"]))
        text = (
            f"# {chunk.get('title', '未命名章节')}\n\n"
            f"- 条目入口：{entry}\n"
            f"- 来源：{source}\n"
        )
        out.write_text(text, encoding="utf-8")

    def _write_file_page(self, parent_file: str, group: list[dict]) -> None:
        p_stem = _parent_stem(parent_file)
        out = self.files_dir / f"{p_stem}.md"

        lines = [
            f"# 文件索引：{parent_file}",
            "",
            f"- 原始文件：{_wikilink(f'raw/{parent_file}'.replace(chr(92), '/'))}",
            "",
        ]
        for c in group:
            lines.append(f"- {_wikilink(str(c['_entry_rel']))}")

        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_tag_page(self, tag: str, group: list[dict]) -> None:
        out = self.tags_dir / f"{_safe_name(tag)}.md"
        lines = [f"# 标签：{tag}", ""]

        seen: set[str] = set()
        for c in group:
            rel = str(c["_entry_rel"])
            if rel in seen:
                continue
            seen.add(rel)
            lines.append(f"- {_wikilink(rel)}")

        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_category_indexes(self, by_category: dict[str, list[dict]]) -> None:
        for cat in self.category_dirs:
            out = self.wiki_root / cat / "_index.md"
            lines = [f"# 分类索引：{cat}", ""]
            items = by_category.get(cat, [])
            if not items:
                lines.append("（当前无条目）")
            else:
                for c in items:
                    lines.append(f"- {_wikilink(str(c['_entry_rel']))}")
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_index(self, parent_files: list[str], by_tag: dict[str, list[dict]]) -> None:
        out = self.wiki_root / "index.md"

        lines = ["# Wiki 索引", "", "## 分类维度", ""]

        category_map: dict[str, list[str]] = defaultdict(list)
        for p in parent_files:
            top = self._raw_top_folder(p)
            cat = self.raw_to_wiki_map.get(top) or "(自动分类)"
            category_map[cat].append(p)

        for cat in sorted(category_map.keys()):
            lines.append(f"### {cat}")
            for p in category_map[cat]:
                lines.append(f"- {_wikilink(f'files/{_parent_stem(p)}')}")
            lines.append("")

        lines.extend(["## 文件维度", ""])
        for p in parent_files:
            lines.append(f"- {_wikilink(f'files/{_parent_stem(p)}')}")

        lines.extend(["", "## 标签维度", ""])
        tag_items = []
        for t in sorted(by_tag.keys()):
            if not self._is_meaningful_tag(t):
                continue
            if len({str(x["chunk_id"]) for x in by_tag[t]}) < 2:
                continue
            tag_items.append(t)
        for t in tag_items[:250]:
            lines.append(f"- {_wikilink(f'tags/{_safe_name(t)}')}")

        out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @classmethod
    def _is_meaningful_tag(cls, tag: str) -> bool:
        t = tag.strip()
        if len(t) < 2 or len(t) > 20:
            return False
        if re.search(r"[^\u4e00-\u9fffA-Za-z0-9_\-]", t):
            return False
        if re.fullmatch(r"[A-Za-z0-9_]{1,2}", t):
            return False
        if re.fullmatch(r"[\u4e00-\u9fff]", t):
            return False
        if t in cls.TAG_STOPWORDS or t.lower() in cls.TAG_STOPWORDS:
            return False
        if t.endswith("的") or t.endswith("了"):
            return False
        return True
