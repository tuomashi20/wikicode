from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from src.utils.config import AppConfig, PROJECT_ROOT


KB_BACKUP_ROOT = PROJECT_ROOT / ".wikicoder" / "kb_backups"


def _snapshot_id(name: str | None = None) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if name and name.strip():
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name.strip())
        return f"{ts}_{safe}"
    return ts


def _copy_tree(src: Path, dst: Path, messages: list[str]) -> None:
    if not src.exists():
        messages.append(f"Skip missing: {src}")
        return
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(p, target)
        except Exception as e:  # noqa: BLE001
            messages.append(f"Skip locked/unreadable file: {p} ({e})")


def save_kb_backup(cfg: AppConfig, name: str | None = None) -> tuple[str, list[str]]:
    KB_BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    bid = _snapshot_id(name)
    root = KB_BACKUP_ROOT / bid
    root.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []

    targets = {
        "raw": cfg.wiki_strategy.raw_path,
        "wiki": cfg.wiki_strategy.wiki_path,
        "processed": cfg.wiki_strategy.processed_path,
    }
    for key, src in targets.items():
        _copy_tree(src, root / key, messages)

    manifest = {
        "id": bid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "raw_path": str(cfg.wiki_strategy.raw_path),
        "wiki_path": str(cfg.wiki_strategy.wiki_path),
        "processed_path": str(cfg.wiki_strategy.processed_path),
    }
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return bid, messages


def list_kb_backups(limit: int = 20) -> list[dict]:
    if not KB_BACKUP_ROOT.exists():
        return []
    out: list[dict] = []
    for d in sorted([x for x in KB_BACKUP_ROOT.iterdir() if x.is_dir()], key=lambda x: x.name, reverse=True):
        mf = d / "manifest.json"
        created = ""
        if mf.exists():
            try:
                created = str((json.loads(mf.read_text(encoding="utf-8")) or {}).get("created_at", ""))
            except Exception:
                created = ""
        out.append({"id": d.name, "created_at": created or d.name, "path": str(d)})
        if len(out) >= limit:
            break
    return out


def restore_kb_backup(cfg: AppConfig, backup_id: str) -> tuple[bool, list[str]]:
    root = KB_BACKUP_ROOT / backup_id
    if not root.exists():
        return False, [f"Backup not found: {backup_id}"]

    messages: list[str] = []
    ok = True
    targets = {
        "raw": cfg.wiki_strategy.raw_path,
        "wiki": cfg.wiki_strategy.wiki_path,
        "processed": cfg.wiki_strategy.processed_path,
    }

    for key, dst in targets.items():
        src = root / key
        if not src.exists():
            messages.append(f"Skip missing backup dir: {src}")
            continue
        if dst.exists():
            for p in sorted(dst.rglob("*"), key=lambda x: len(x.parts), reverse=True):
                try:
                    if p.is_file():
                        p.unlink()
                    elif p.is_dir():
                        p.rmdir()
                except Exception:
                    pass
        try:
            dst.mkdir(parents=True, exist_ok=True)
            _copy_tree(src, dst, messages)
            messages.append(f"Restored {key}: {dst}")
        except Exception as e:  # noqa: BLE001
            ok = False
            messages.append(f"Failed restoring {key}: {e}")
    return ok, messages

