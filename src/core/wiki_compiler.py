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
        ws = config.wiki_strategy
        self.comparison_hints = tuple([str(x).lower() for x in ws.comparison_hints if str(x).strip()])
        self.concept_cues = tuple([str(x) for x in ws.concept_cues if str(x).strip()])
        self.entity_org_suffixes = tuple([str(x) for x in ws.entity_org_suffixes if str(x).strip()])
        self.entity_type_hints = tuple([str(x) for x in ws.entity_type_hints if str(x).strip()])
        self.entity_exclude_terms = tuple([str(x) for x in ws.entity_exclude_terms if str(x).strip()])
        self.entity_content_cues = tuple([str(x) for x in ws.entity_content_cues if str(x).strip()])
        self.entity_ignore_terms = set(str(x).strip() for x in ws.entity_ignore_terms if str(x).strip())
        self.entity_card_min_mentions = max(1, int(ws.entity_card_min_mentions))
        self.entity_card_max_pages = max(1, int(ws.entity_card_max_pages))
        self.entity_card_name_max_len = max(8, int(ws.entity_card_name_max_len))
        self.chapter_title_patterns = tuple([str(x) for x in ws.chapter_title_patterns if str(x).strip()])
        self.chapter_exact_terms = set(str(x) for x in ws.chapter_exact_terms if str(x).strip())
        self.tag_stopwords = set(str(x).strip().lower() for x in ws.tag_stopwords if str(x).strip())
        self.tag_block_patterns = tuple([str(x) for x in ws.tag_block_patterns if str(x).strip()])
        self.tag_block_prefixes = tuple([str(x).strip().lower() for x in ws.tag_block_prefixes if str(x).strip()])
        self.tag_min_len = max(1, int(ws.tag_min_len))
        self.tag_max_len = max(self.tag_min_len, int(ws.tag_max_len))
        self.entity_cards_dir = self.wiki_root / "entities"
        self._entity_card_rels: list[str] = []

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
                if str(c.get("_category", "")) != "entities":
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

        entity_cards = self._build_entity_cards(chunks)
        self._write_entity_cards(entity_cards)

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
        if mapped in {"entries", "comparisons", "queries"}:
            return mapped

        title = str(chunk.get("title", ""))
        tags = str(chunk.get("tags", ""))
        content = str(chunk.get("content_text", ""))[:500]
        text = f"{title} {tags} {content}".lower()
        chapter_like = self._is_chapter_like(title)

        if any(k in text for k in self.comparison_hints):
            return "comparisons" if "comparisons" in self.category_dirs else "queries"
        if "?" in text or "？" in text or "什么" in text or "如何" in text:
            return "queries"

        if (not chapter_like) and self._looks_like_concept(title, content):
            return "concepts" if "concepts" in self.category_dirs else "entries"
        if (not chapter_like) and self._looks_like_entity(title, content):
            return "entities" if "entities" in self.category_dirs else "entries"
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
            if cat == "entities" and self._entity_card_rels:
                lines.append("## 实体卡片")
                lines.append("")
                for rel in self._entity_card_rels:
                    lines.append(f"- {_wikilink(rel)}")
                lines.append("")
            items = by_category.get(cat, [])
            if not items:
                lines.append("（当前无条目）")
            else:
                if cat == "entities":
                    lines.append("## 关联条目")
                    lines.append("")
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

    def _is_meaningful_tag(self, tag: str) -> bool:
        t = tag.strip()
        if len(t) < self.tag_min_len or len(t) > self.tag_max_len:
            return False
        if self._is_chapter_like(t):
            return False
        tl = t.lower()
        for prefix in self.tag_block_prefixes:
            if tl.startswith(prefix):
                return False
        for pat in self.tag_block_patterns:
            try:
                if re.fullmatch(pat, t, flags=re.IGNORECASE):
                    return False
            except re.error:
                continue
        if re.search(r"[^\u4e00-\u9fffA-Za-z0-9_\-]", t):
            return False
        if re.fullmatch(r"[A-Za-z0-9_]{1,2}", t):
            return False
        if re.fullmatch(r"[\u4e00-\u9fff]", t):
            return False
        if tl in self.tag_stopwords:
            return False
        if t.endswith("的") or t.endswith("了"):
            return False
        return True

    def _is_chapter_like(self, text: str) -> bool:
        t = text.strip()
        if not t:
            return False
        for pat in self.chapter_title_patterns:
            try:
                if re.fullmatch(pat, t):
                    return True
            except re.error:
                continue
        if t in self.chapter_exact_terms:
            return True
        return False

    def _looks_like_concept(self, title: str, content: str) -> bool:
        txt = f"{title}\n{content[:700]}"
        if any(k in txt for k in self.concept_cues):
            return True
        return False

    def _looks_like_entity(self, title: str, content: str) -> bool:
        t = title.strip()
        if not t:
            return False
        if any(x in t for x in self.entity_exclude_terms):
            return False
        if any(sfx in t for sfx in self.entity_org_suffixes):
            if len(t) <= 40:
                return True
        if any(k in t for k in self.entity_type_hints) and len(t) <= 24:
            return True
        c = content[:220]
        if any(k in c for k in self.entity_content_cues):
            return True
        return False

    def _extract_entities_from_text(self, text: str) -> list[str]:
        if not text.strip():
            return []
        suffix_union = sorted(set(self.entity_org_suffixes + self.entity_type_hints), key=len, reverse=True)
        if not suffix_union:
            return []
        suffix_pat = "|".join(re.escape(x) for x in suffix_union)
        pattern = re.compile(rf"([\u4e00-\u9fffA-Za-z0-9\-]{{1,{self.entity_card_name_max_len}}}?(?:{suffix_pat}))")
        raw = [m.group(1) for m in pattern.finditer(text)]
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            n = item.strip("，。；：,.:;（）()[]【】 ")
            if not n:
                continue
            if len(n) < 2 or len(n) > self.entity_card_name_max_len:
                continue
            if self._is_chapter_like(n):
                continue
            if any(x in n for x in self.entity_exclude_terms):
                continue
            if n in self.entity_ignore_terms:
                continue
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
        return out

    def _infer_entity_type(self, name: str) -> str:
        if any(s in name for s in self.entity_org_suffixes):
            return "组织"
        if any(s in name for s in self.entity_type_hints):
            return "系统/对象"
        return "实体"

    def _build_entity_cards(self, chunks: list[dict]) -> list[dict]:
        entity_map: dict[str, dict] = {}
        for c in chunks:
            title = str(c.get("title", ""))
            content = str(c.get("content_text", ""))[:1800]
            names = self._extract_entities_from_text(f"{title}\n{content}")
            if not names:
                continue
            entry_rel = str(c.get("_entry_rel", ""))
            chunk_id = str(c.get("chunk_id", ""))
            for n in names:
                obj = entity_map.setdefault(
                    n,
                    {
                        "name": n,
                        "type": self._infer_entity_type(n),
                        "entries": set(),
                        "chunks": set(),
                        "co": defaultdict(int),
                    },
                )
                if entry_rel:
                    obj["entries"].add(entry_rel)
                if chunk_id:
                    obj["chunks"].add(chunk_id)
            for i, a in enumerate(names):
                for j, b in enumerate(names):
                    if i == j:
                        continue
                    entity_map[a]["co"][b] += 1

        cards: list[dict] = []
        for name, obj in entity_map.items():
            if len(obj["chunks"]) < self.entity_card_min_mentions:
                continue
            rel = f"entities/{_safe_name(name)}"
            co_sorted = sorted(obj["co"].items(), key=lambda x: x[1], reverse=True)
            cards.append(
                {
                    "name": name,
                    "type": obj["type"],
                    "entries": sorted(obj["entries"]),
                    "mentions": len(obj["chunks"]),
                    "related_entities": [x for x, _ in co_sorted[:12]],
                    "rel": rel,
                }
            )
        cards.sort(key=lambda x: (-int(x["mentions"]), str(x["name"])))
        return cards[: self.entity_card_max_pages]

    def _write_entity_cards(self, cards: list[dict]) -> None:
        self._entity_card_rels = []
        if not cards:
            return
        self.entity_cards_dir.mkdir(parents=True, exist_ok=True)
        name_to_rel = {str(c["name"]): str(c["rel"]) for c in cards}
        for c in cards:
            rel = str(c["rel"])
            out = self.wiki_root / f"{rel}.md"
            self._entity_card_rels.append(rel)
            entries = [str(x) for x in c.get("entries", [])][:20]
            rel_entities = [str(x) for x in c.get("related_entities", []) if str(x) in name_to_rel][:12]
            lines = [
                f"# 实体：{c.get('name', '')}",
                "",
                f"- 类型：{c.get('type', '实体')}",
                f"- 关联条目数：{len(entries)}",
                f"- 出现次数（chunk）：{c.get('mentions', 0)}",
                "",
                "## 关联条目",
                "",
            ]
            if entries:
                for e in entries:
                    lines.append(f"- {_wikilink(e)}")
            else:
                lines.append("（无）")
            lines.extend(["", "## 关联实体", ""])
            if rel_entities:
                for n in rel_entities:
                    lines.append(f"- {_wikilink(name_to_rel[n])}")
            else:
                lines.append("（无）")
            out.write_text("\n".join(lines) + "\n", encoding="utf-8")
