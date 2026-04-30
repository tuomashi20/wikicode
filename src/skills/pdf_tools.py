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

def convert_pdf_file_to_markdown(pdf_path: Path) -> Path:
    """使用 pdfplumber 进行高质量 Markdown 转换（支持表格）"""
    import pdfplumber  # lazy import
    from pypdf import PdfReader # 图片提取仍需 pypdf 支持

    assets_dir = _global_assets_dir()
    prefix = _asset_prefix(pdf_path)
    lines: list[str] = [f"# {pdf_path.stem}", ""]
    
    # 使用 pypdf 提取图片
    image_reader = PdfReader(str(pdf_path))
    
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            lines.append(f"## 第 {i} 页")
            lines.append("")
            
            # 1. 提取表格并转为 Markdown
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    if not table: continue
                    # 清理 None 值
                    clean_table = [[(str(cell).replace("\n", " ") if cell else "") for cell in row] for row in table]
                    if not clean_table: continue
                    
                    # 生成 Markdown 表格
                    cols = len(clean_table[0])
                    lines.append("| " + " | ".join(clean_table[0]) + " |")
                    lines.append("| " + " | ".join(["---"] * cols) + " |")
                    for row in clean_table[1:]:
                        lines.append("| " + " | ".join(row) + " |")
                    lines.append("")
            
            # 2. 提取并清洗文本 (layout=True 保留部分结构)
            text = page.extract_text(layout=True)
            if text:
                # 简单的行清洗，避免过度留白
                clean_lines = [l.strip() for l in text.split("\n") if l.strip()]
                lines.extend(clean_lines)
            else:
                if not tables:
                    lines.append("_（该页未提取到文本，可能是扫描件或纯图）_")
            
            lines.append("")

            # 3. 提取图片 (沿用原逻辑)
            if i <= len(image_reader.pages):
                img_page = image_reader.pages[i-1]
                from src.skills.pdf_tools import _extract_page_images
                image_files = _extract_page_images(img_page, assets_dir, i, prefix)
                if image_files:
                    lines.append("### 本页图片")
                    for p in image_files:
                        rel = _rel_link(p, pdf_path.parent)
                        lines.append(f"![{p.name}]({rel})")
                    lines.append("")

    out = pdf_path.with_suffix(".md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out

def _extract_page_images(page, assets_dir: Path, page_no: int, prefix: str) -> list[Path]:
    out: list[Path] = []
    images = getattr(page, "images", None)
    if not images: return out
    for idx, img in enumerate(images, start=1):
        data = getattr(img, "data", None)
        if not data: continue
        ext = Path(str(getattr(img, "name", ""))).suffix.lower() or ".jpg"
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}: ext = ".jpg"
        out_path = assets_dir / f"{prefix}_p{page_no:04d}_{idx:02d}{ext}"
        out_path.write_bytes(data)
        out.append(out_path)
    return out

def convert_pdf_path(path_str: str, recursive: bool = False) -> tuple[list[Path], list[str]]:
    p = Path(path_str).expanduser()
    if not p.is_absolute(): p = (Path.cwd() / p).resolve()
    if not p.exists(): return [], [f"路径不存在：{p}"]

    if p.is_file():
        if p.suffix.lower() != ".pdf": return [], [f"不支持的文件: {p}"]
        try: return [convert_pdf_file_to_markdown(p)], []
        except Exception as e: return [], [f"转换失败: {p} | {e}"]

    globber = p.rglob if recursive else p.glob
    files = [f for f in globber("*.pdf") if f.is_file()]
    outs, errs = [], []
    for f in sorted(files):
        try: outs.append(convert_pdf_file_to_markdown(f))
        except Exception as e: errs.append(f"转换失败: {f} | {e}")
    return outs, errs
