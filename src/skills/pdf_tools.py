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


def _extract_page_images(page, assets_dir: Path, page_no: int, prefix: str) -> list[Path]:
    out: list[Path] = []
    images = getattr(page, "images", None)
    if images is None:
        return out
    idx = 0
    for img in images:
        idx += 1
        data = getattr(img, "data", None)
        name = str(getattr(img, "name", "") or "")
        if not data:
            continue
        ext = Path(name).suffix.lower()
        if ext not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
            ext = ".bin"
        out_path = assets_dir / f"{prefix}_p{page_no:04d}_{idx:02d}{ext}"
        out_path.write_bytes(data)
        out.append(out_path)
    return out


def convert_pdf_file_to_markdown(pdf_path: Path) -> Path:
    from pypdf import PdfReader  # lazy import

    reader = PdfReader(str(pdf_path))
    lines: list[str] = [f"# {pdf_path.stem}", ""]
    assets_dir = _global_assets_dir()
    prefix = _asset_prefix(pdf_path)

    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        lines.append(f"## 第 {i} 页")
        lines.append("")
        if text:
            lines.append(text)
        else:
            lines.append("_（该页未提取到文本，可能是扫描页/图片页）_")
        lines.append("")

        image_files = _extract_page_images(page, assets_dir, i, prefix)
        if image_files:
            lines.append("### 本页图片")
            lines.append("")
            for p in image_files:
                rel = _rel_link(p, pdf_path.parent)
                lines.append(f"![{p.name}]({rel})")
            lines.append("")

    out = pdf_path.with_suffix(".md")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def convert_pdf_path(path_str: str, recursive: bool = False) -> tuple[list[Path], list[str]]:
    p = Path(path_str).expanduser()
    if not p.is_absolute():
        p = (Path.cwd() / p).resolve()
    if not p.exists():
        return [], [f"路径不存在：{p}"]

    if p.is_file():
        if p.suffix.lower() != ".pdf":
            return [], [f"不是可处理的 pdf 文件：{p}"]
        try:
            return [convert_pdf_file_to_markdown(p)], []
        except Exception as e:  # noqa: BLE001
            return [], [f"转换失败：{p} | {e}"]

    globber = p.rglob if recursive else p.glob
    files = [f for f in globber("*.pdf") if f.is_file()]
    if not files:
        return [], [f"目录下未找到 pdf：{p}"]

    outs: list[Path] = []
    errs: list[str] = []
    for f in sorted(files):
        try:
            outs.append(convert_pdf_file_to_markdown(f))
        except Exception as e:  # noqa: BLE001
            errs.append(f"转换失败：{f} | {e}")
    return outs, errs
