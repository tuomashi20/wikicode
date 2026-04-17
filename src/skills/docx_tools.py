from __future__ import annotations

import hashlib
import os
from pathlib import Path


def _global_assets_dir() -> Path:
    env_dir = os.getenv("WIKICODER_MD_ASSETS_DIR", "").strip()
    if env_dir:
        d = Path(env_dir).expanduser()
        if not d.is_absolute():
            d = (Path.cwd() / d).resolve()
    else:
        d = (Path.cwd() / "data" / "md_assets").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _asset_prefix(base_file: Path) -> str:
    digest = hashlib.md5(str(base_file.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"{base_file.stem}_{digest}"


def _rel_link(asset_path: Path, source_parent: Path) -> str:
    try:
        return os.path.relpath(asset_path, start=source_parent).replace("\\", "/")
    except Exception:
        return asset_path.as_posix()


def _iter_block_items(doc):
    from docx.table import Table  # lazy import
    from docx.text.paragraph import Paragraph  # lazy import

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.lower()
        if tag.endswith("}p"):
            yield Paragraph(child, doc)
        elif tag.endswith("}tbl"):
            yield Table(child, doc)


def _extract_paragraph_images(paragraph, assets_dir: Path, saved: dict[str, Path], prefix: str) -> list[Path]:
    from docx.oxml.ns import qn  # lazy import

    out: list[Path] = []
    for blip in paragraph._p.xpath(".//a:blip"):
        rid = blip.get(qn("r:embed"))
        if not rid:
            continue
        part = paragraph.part.related_parts.get(rid)
        if part is None or not hasattr(part, "blob"):
            continue
        blob = part.blob
        if not blob:
            continue
        digest = hashlib.sha1(blob).hexdigest()
        if digest in saved:
            out.append(saved[digest])
            continue
        ext = Path(str(getattr(part, "partname", "") or "")).suffix.lower() or ".bin"
        out_path = assets_dir / f"{prefix}_{len(saved)+1:03d}{ext}"
        out_path.write_bytes(blob)
        saved[digest] = out_path
        out.append(out_path)
    return out


def convert_docx_file_to_markdown(docx_path: Path) -> Path:
    from docx import Document  # lazy import

    doc = Document(str(docx_path))
    lines: list[str] = [f"# {docx_path.stem}", ""]
    assets_dir = _global_assets_dir()
    prefix = _asset_prefix(docx_path)
    saved_images: dict[str, Path] = {}

    table_idx = 0
    for block in _iter_block_items(doc):
        block_type = type(block).__name__.lower()
        if "paragraph" in block_type:
            p = block
            text = (p.text or "").strip()
            style_name = (p.style.name or "").lower() if p.style is not None else ""
            if text:
                if "heading" in style_name:
                    level = 2
                    for ch in reversed(style_name):
                        if ch.isdigit():
                            level = max(2, min(6, int(ch) + 1))
                            break
                    lines.append(f"{'#' * level} {text}")
                else:
                    lines.append(text)
                lines.append("")
            imgs = _extract_paragraph_images(p, assets_dir, saved_images, prefix)
            for img in imgs:
                rel = _rel_link(img, docx_path.parent)
                lines.append(f"![{img.name}]({rel})")
            if imgs:
                lines.append("")
            continue

        table_idx += 1
        table = block
        lines.append(f"## 表格 {table_idx}")
        lines.append("")
        rows = []
        max_cols = 0
        for row in table.rows:
            vals = [(c.text or "").replace("\n", "<br>").replace("|", "\\|").strip() for c in row.cells]
            while vals and vals[-1] == "":
                vals.pop()
            if len(vals) > max_cols:
                max_cols = len(vals)
            rows.append(vals)
        if max_cols == 0:
            lines.append("_（空表）_")
            lines.append("")
            continue
        rows = [r + [""] * (max_cols - len(r)) for r in rows]
        header = rows[0] if rows else [f"col_{i+1}" for i in range(max_cols)]
        header = [h if h else f"col_{i+1}" for i, h in enumerate(header)]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for r in rows[1:]:
            lines.append("| " + " | ".join(r) + " |")
        lines.append("")

    out = docx_path.with_suffix(".md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def convert_docx_path(path_str: str, recursive: bool = False) -> tuple[list[Path], list[str]]:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        return [], [f"路径不存在：{p}"]

    if p.is_file():
        if p.suffix.lower() != ".docx" or p.name.startswith("~$"):
            return [], [f"不是可处理的 docx 文件：{p}"]
        try:
            return [convert_docx_file_to_markdown(p)], []
        except Exception as e:  # noqa: BLE001
            return [], [f"转换失败：{p} | {e}"]

    globber = p.rglob if recursive else p.glob
    files = [f for f in globber("*.docx") if f.is_file() and not f.name.startswith("~$")]
    if not files:
        return [], [f"目录下未找到 docx：{p}"]

    outs: list[Path] = []
    errs: list[str] = []
    for f in sorted(files):
        try:
            outs.append(convert_docx_file_to_markdown(f))
        except Exception as e:  # noqa: BLE001
            errs.append(f"转换失败：{f} | {e}")
    return outs, errs
