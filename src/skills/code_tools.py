from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from src.utils.config import PROJECT_ROOT


BACKUP_ROOT = PROJECT_ROOT / ".wikicoder" / "backups"


@dataclass
class PatchSummary:
    file: str
    added: int
    removed: int
    hunks: int



def _safe_path(path: str) -> Path:
    p = (PROJECT_ROOT / path).resolve()
    if not str(p).startswith(str(PROJECT_ROOT.resolve())):
        raise ValueError("Path escapes project root")
    return p



def _normalize_diff_path(path: str) -> str:
    p = path.strip()
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p



def read_file(path: str) -> str:
    p = _safe_path(path)
    return p.read_text(encoding="utf-8", errors="ignore") if p.exists() else ""



def write_file(path: str, content: str) -> None:
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")



def patch_apply(path: str, old: str, new: str) -> bool:
    p = _safe_path(path)
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8", errors="ignore")
    if old not in text:
        return False
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return True



def extract_unified_diff(text: str) -> str:
    s = text.strip()
    if not s:
        return ""

    fenced = re.findall(r"```(?:diff|patch)?\n([\s\S]*?)\n```", s, flags=re.IGNORECASE)
    for block in fenced:
        if "@@" in block and ("+++" in block or "---" in block):
            return block.strip()

    if "@@" in s and ("+++" in s or "---" in s):
        return s
    return ""



def _split_diff_blocks(diff_text: str) -> list[tuple[str, str, str]]:
    lines = diff_text.splitlines()
    blocks: list[tuple[str, str, str]] = []

    i = 0
    while i < len(lines):
        if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old_path = lines[i][4:].strip()
            new_path = lines[i + 1][4:].strip()
            start = i
            i += 2
            while i < len(lines):
                if lines[i].startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    break
                i += 1
            block = "\n".join(lines[start:i])
            blocks.append((old_path, new_path, block))
            continue
        i += 1

    if blocks:
        return blocks

    if any(ln.startswith("@@") for ln in lines):
        return [("", "", diff_text)]
    return []



def summarize_unified_diff(text: str) -> list[PatchSummary]:
    diff = extract_unified_diff(text)
    if not diff:
        return []

    summaries: list[PatchSummary] = []
    for old_path, new_path, block in _split_diff_blocks(diff):
        target = _normalize_diff_path(new_path or old_path)
        added = removed = hunks = 0
        for ln in block.splitlines():
            if ln.startswith("@@"):
                hunks += 1
            elif ln.startswith("+") and not ln.startswith("+++"):
                added += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                removed += 1
        summaries.append(PatchSummary(file=target, added=added, removed=removed, hunks=hunks))
    return summaries



def _apply_unified_diff_block(path: str, block_text: str) -> tuple[bool, str]:
    target = _safe_path(path)
    if not target.exists():
        return False, f"Target file not found: {path}"

    lines = block_text.splitlines()
    hunk_starts = [i for i, ln in enumerate(lines) if ln.startswith("@@")]
    if not hunk_starts:
        return False, "No hunk found in diff."

    original_text = target.read_text(encoding="utf-8", errors="ignore")
    orig = original_text.splitlines()
    out: list[str] = []
    src_idx = 0

    def parse_old_start(hdr: str) -> int:
        m = re.match(r"@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@", hdr)
        if not m:
            raise ValueError(f"Invalid hunk header: {hdr}")
        return int(m.group(1))

    for hs_i, hs in enumerate(hunk_starts):
        old_start = parse_old_start(lines[hs])
        next_hs = hunk_starts[hs_i + 1] if hs_i + 1 < len(hunk_starts) else len(lines)
        hunk_lines = lines[hs + 1 : next_hs]

        copy_until = old_start - 1
        if copy_until < src_idx:
            return False, "Overlapping hunks or invalid patch positions."

        out.extend(orig[src_idx:copy_until])
        src_idx = copy_until

        for hl in hunk_lines:
            if not hl:
                marker = " "
                payload = ""
            else:
                marker, payload = hl[0], hl[1:]

            if marker == " ":
                if src_idx >= len(orig) or orig[src_idx] != payload:
                    return False, f"Context mismatch near: {payload[:80]}"
                out.append(orig[src_idx])
                src_idx += 1
            elif marker == "-":
                if src_idx >= len(orig) or orig[src_idx] != payload:
                    return False, f"Delete mismatch near: {payload[:80]}"
                src_idx += 1
            elif marker == "+":
                out.append(payload)
            elif hl.startswith("\\ No newline at end of file"):
                continue
            else:
                return False, f"Unsupported diff line: {hl[:80]}"

    out.extend(orig[src_idx:])
    keep_trailing_newline = original_text.endswith("\n")
    new_text = "\n".join(out)
    if keep_trailing_newline:
        new_text += "\n"
    target.write_text(new_text, encoding="utf-8")
    return True, f"Applied patch to {path}"



def apply_unified_diff(path: str, patch_text: str) -> tuple[bool, str]:
    diff = extract_unified_diff(patch_text)
    if not diff:
        return False, "No unified diff content found."

    blocks = _split_diff_blocks(diff)
    if not blocks:
        return False, "No patch block found."

    norm_target = _normalize_diff_path(path)

    for old_path, new_path, block in blocks:
        candidates = {_normalize_diff_path(old_path), _normalize_diff_path(new_path)}
        if norm_target in candidates:
            return _apply_unified_diff_block(path, block)

    if len(blocks) == 1:
        return _apply_unified_diff_block(path, blocks[0][2])

    return False, f"Patch has {len(blocks)} files but none matches target: {path}"



def apply_unified_diff_multi(patch_text: str, allowed_files: set[str] | None = None) -> tuple[bool, list[str]]:
    diff = extract_unified_diff(patch_text)
    if not diff:
        return False, ["No unified diff content found."]

    blocks = _split_diff_blocks(diff)
    if not blocks:
        return False, ["No patch block found."]

    messages: list[str] = []
    all_ok = True

    for old_path, new_path, block in blocks:
        target = _normalize_diff_path(new_path or old_path)
        if not target or target == "/dev/null":
            all_ok = False
            messages.append("Skip unsupported create/delete file block.")
            continue

        if allowed_files is not None and target not in allowed_files:
            all_ok = False
            messages.append(f"Skip not-allowed file: {target}")
            continue

        ok, msg = _apply_unified_diff_block(target, block)
        all_ok = all_ok and ok
        messages.append(msg)

    return all_ok, messages



def create_backup(files: list[str]) -> tuple[bool, str, list[str]]:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    backup_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    backup_dir = BACKUP_ROOT / backup_id
    backup_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, object] = {
        "id": backup_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "files": [],
    }

    messages: list[str] = []
    for rel in files:
        rel_norm = rel.replace("\\", "/")
        src = _safe_path(rel_norm)
        file_info = {"path": rel_norm, "exists": src.exists()}
        if src.exists():
            dst = backup_dir / rel_norm
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            messages.append(f"Backed up: {rel_norm}")
        else:
            messages.append(f"Skip backup (not found): {rel_norm}")
        manifest["files"].append(file_info)

    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return True, backup_id, messages



def restore_backup(backup_id: str) -> tuple[bool, list[str]]:
    backup_dir = BACKUP_ROOT / backup_id
    manifest_file = backup_dir / "manifest.json"
    if not manifest_file.exists():
        return False, [f"Backup not found: {backup_id}"]

    data = json.loads(manifest_file.read_text(encoding="utf-8"))
    files = data.get("files", [])
    messages: list[str] = []
    all_ok = True

    for item in files:
        rel = str(item.get("path", ""))
        existed = bool(item.get("exists", False))
        target = _safe_path(rel)
        saved = backup_dir / rel

        if existed:
            if not saved.exists():
                all_ok = False
                messages.append(f"Missing backup copy: {rel}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(saved, target)
            messages.append(f"Restored: {rel}")
        else:
            if target.exists():
                target.unlink()
                messages.append(f"Removed (was absent in backup): {rel}")
            else:
                messages.append(f"No-op: {rel}")

    return all_ok, messages



def list_backups(limit: int = 20) -> list[dict[str, str]]:
    if not BACKUP_ROOT.exists():
        return []

    items: list[dict[str, str]] = []
    for d in sorted([x for x in BACKUP_ROOT.iterdir() if x.is_dir()], reverse=True)[:limit]:
        manifest_file = d / "manifest.json"
        created = ""
        file_count = "0"
        if manifest_file.exists():
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                created = str(data.get("created_at", ""))
                file_count = str(len(data.get("files", [])))
            except Exception:
                pass
        items.append({"id": d.name, "created_at": created, "file_count": file_count})
    return items
