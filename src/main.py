from __future__ import annotations

import ast
import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import typer
import yaml
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown

try:
    import msvcrt  # type: ignore[attr-defined]
except Exception:  # pragma: no cover
    msvcrt = None
try:
    import ctypes  # type: ignore
except Exception:  # pragma: no cover
    ctypes = None
try:
    import select
    import termios
    import tty
except Exception:  # pragma: no cover
    select = None
    termios = None
    tty = None

from src.core.agent import AgentResponse, WikiFirstAgent
from src.core.build_agent import BuildAgent, BuildStep
from src.core.atomizer import Atomizer
from src.core.llm_client import LLMClient
from src.core.retrieval_eval import (
    compare_eval_reports,
    evaluate_retrieval,
    load_eval_cases,
    load_eval_report,
    save_eval_report,
)
from src.skills.code_tools import (
    apply_unified_diff,
    apply_unified_diff_multi,
    create_backup,
    list_backups,
    read_file,
    restore_backup,
    summarize_unified_diff,
    write_file,
)
from src.skills.docx_tools import convert_docx_path
from src.skills.pdf_tools import convert_pdf_path
from src.skills.xlsx_tools import convert_xlsx_path
from src.skills.wiki_tools import wiki_list_structure
from src.utils.config import AppConfig, DEFAULT_CONFIG_PATH, PROJECT_ROOT, ensure_workspace, load_config
from src.utils.kb_backup import list_kb_backups, restore_kb_backup, save_kb_backup
from src.utils.db_manager import clear_index_store, resolve_db_path


app = typer.Typer(help="WikiCoder CLI")
console = Console()
SESSION_STATE_PATH = PROJECT_ROOT / ".wikicoder" / "session_state.json"

CLI_BANNER = r"""
██╗    ██╗██╗██╗  ██╗██╗ ██████╗ ██████╗ ██████╗ ███████╗██████╗
██║    ██║██║██║ ██╔╝██║██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗
██║ █╗ ██║██║█████╔╝ ██║██║     ██║   ██║██║  ██║█████╗  ██████╔╝
██║███╗██║██║██╔═██╗ ██║██║     ██║   ██║██║  ██║██╔══╝  ██╔══██╗
╚███╔███╔╝██║██║  ██╗██║╚██████╗╚██████╔╝██████╔╝███████╗██║  ██║
 ╚══╝╚══╝ ╚═╝╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝  ╚═╝
"""


class SlashCommandCompleter(Completer):
    def __init__(self) -> None:
        self.commands = [
            ("/help", "查看命令帮助"),
            ("/sync", "同步知识库（RAW -> WIKI）"),
            ("/kbclear yes", "清空索引（需确认）"),
            ("/kbclear all yes", "清空索引 + Wiki 页面（保留 Raw）"),
            ("/kbsave ", "备份知识库（raw/wiki/processed）"),
            ("/kbbackups", "查看知识库备份列表"),
            ("/kbrestore ", "恢复知识库备份"),
            ("/vaultpath ", "设置知识库根目录"),
            ("/ask ", "强制 Wiki 模式提问"),
            ("/structure", "查看索引结构"),
            ("/model", "查看/切换模型配置"),
            ("/mode ", "切换会话模式"),
            ("/resume", "继续上次会话上下文"),
            ("/reset", "清空会话记忆"),
            ("/memdraft ", "将最近对话整理为wiki草稿"),
            ("/memsave ", "保存wiki草稿到raw/faq"),
            ("/xlsx2md ", "xlsx 转 markdown（文件或目录）"),
            ("/pdf2md ", "pdf 转 markdown（文件或目录）"),
            ("/docx2md ", "word 转 markdown（文件或目录）"),
            ("/md2canvas ", "markdown 转 obsidian canvas（正则版）"),
            ("/md2canvas_ai ", "markdown 转 obsidian canvas（AI 增强版）"),
            ("/exit", "退出 CLI"),
            ("/help advanced", "查看高级命令"),
        ]

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        for cmd, desc in self.commands:
            if cmd.startswith(text):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


def build_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("escape")
    def _(event):
        buf = event.app.current_buffer
        if buf.complete_state:
            buf.cancel_completion()

    @kb.add("enter")
    def _(event):
        buf = event.app.current_buffer
        if buf.complete_state and buf.complete_state.current_completion is not None:
            # 回车：选中当前下拉项后直接发送命令
            buf.apply_completion(buf.complete_state.current_completion)
            buf.validate_and_handle()
            return
        # 若已弹出补全但未显式选中，回车默认选择第一项并执行
        if buf.complete_state and getattr(buf.complete_state, "completions", None):
            first = buf.complete_state.completions[0]
            if first is not None:
                buf.apply_completion(first)
                buf.validate_and_handle()
                return
        # 若没有弹层但输入是斜杠命令前缀，自动补全唯一命令并执行（如 /e -> /exit）
        text = (buf.text or "").strip()
        comp = getattr(buf, "completer", None)
        cmd_list = getattr(comp, "commands", None)
        if text.startswith("/") and isinstance(cmd_list, list):
            matches = [c for c, _ in cmd_list if c.startswith(text)]
            if len(matches) == 1:
                buf.text = matches[0]
                buf.cursor_position = len(matches[0])
                buf.validate_and_handle()
                return
        buf.validate_and_handle()

    return kb


def _escape_pressed() -> bool:
    # Windows
    if os.name == "nt":
        # 1) 直接查键盘状态（更稳定）
        try:
            if ctypes is not None and bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000):  # VK_ESCAPE
                return True
        except Exception:
            pass

        # 2) 读取控制台缓冲区按键（兼容不同输入法/终端）
        if msvcrt is None:
            return False
        pressed = False
        while msvcrt.kbhit():
            ch = msvcrt.getch()
            if ch in (b"\x00", b"\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getch()
                continue
            if ch in (b"\x1b",):
                pressed = True
        return pressed

    # Linux/macOS (needs stdin in cbreak/raw mode to be truly immediate)
    if os.name != "nt" and select is not None and sys.stdin and sys.stdin.isatty():
        pressed = False
        try:
            while True:
                r, _, _ = select.select([sys.stdin], [], [], 0)
                if not r:
                    break
                ch = os.read(sys.stdin.fileno(), 1)
                if ch == b"\x1b":
                    pressed = True
            return pressed
        except Exception:
            return False
    return False


def _enable_posix_cbreak_if_needed():
    if os.name == "nt" or termios is None or tty is None:
        return None
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        return (fd, old)
    except Exception:
        return None


def _restore_posix_terminal(state) -> None:
    if not state or termios is None:
        return
    try:
        fd, old = state
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _print_startup_banner() -> None:
    console.clear()
    console.print(f"[bold cyan]{CLI_BANNER}[/bold cyan]")
    console.print("[bold cyan]wikicoder[/bold cyan]")
    console.print("[bold]wikicoder cli[/bold]  输入 /help 查看详细命令")


def _safe_filename(name: str, default: str = "memory_note") -> str:
    s = re.sub(r"[\\/:*?\"<>|]+", "_", (name or "").strip())
    s = re.sub(r"\s+", "_", s).strip("._")
    return s or default


def _save_memory_markdown(config: AppConfig, title: str, markdown_text: str) -> Path:
    raw_root = config.wiki_strategy.raw_path
    faq_dir = raw_root / "faq"
    faq_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{_safe_filename(title)}.md"
    out = faq_dir / filename
    out.write_text(markdown_text, encoding="utf-8")
    return out


def _looks_like_image_generate_request(text: str) -> bool:
    t = text.strip().lower()
    keys = ["生成图片", "画一张", "帮我画", "生成一张", "做一张图", "出一张图", "image generate", "draw"]
    if any(k in t for k in keys):
        return True
    return False


def _extract_image_generate_prompt(text: str) -> str:
    s = text.strip()
    s = re.sub(r"^(请|麻烦|帮我|请帮我)\s*", "", s)
    s = re.sub(r"(生成|画|绘制)(一张|个|幅)?", "", s)
    s = s.replace("图片", "").replace("图像", "").strip(" ：:，,。")
    return s or text.strip()


def _looks_like_kb_save_request(text: str) -> bool:
    t = text.strip().lower()
    keys = [
        "写入本地知识库",
        "写入知识库",
        "保存到知识库",
        "存入知识库",
        "写入wiki",
        "保存到wiki",
    ]
    return any(k in t for k in keys)


def _extract_kb_title(text: str) -> str:
    s = text.strip()
    m = re.search(r"(标题|title)\s*[:：]\s*(.+)$", s, flags=re.IGNORECASE)
    if m:
        return m.group(2).strip()
    return ""


def _extract_kb_content(text: str) -> str:
    s = text.strip()
    # 优先提取代码块中的正文
    m_block = re.search(r"```(?:markdown|md|text)?\s*([\s\S]*?)\s*```", s, flags=re.IGNORECASE)
    if m_block:
        return m_block.group(1).strip()

    # 其次提取“内容: ...”
    m = re.search(r"(内容|正文|content)\s*[:：]\s*([\s\S]+)$", s, flags=re.IGNORECASE)
    if m:
        return m.group(2).strip()
    return ""


def _build_kb_markdown_from_last_turn(history: list[tuple[str, str]], title_hint: str = "") -> tuple[str, str]:
    if not history:
        return "", ""
    q, a = history[-1]
    title = title_hint.strip() or _safe_filename(q[:30], default="会话总结")
    md = (
        f"# {title}\n\n"
        "## 背景问题\n\n"
        f"{q}\n\n"
        "## 总结内容\n\n"
        f"{a}\n\n"
        "## 标签\n\n"
        "- 会话沉淀\n"
        "- 自动入库\n"
    )
    return title, md


def _normalize_kb_markdown(content: str, title_hint: str = "") -> tuple[str, str]:
    raw = (content or "").strip()
    if not raw:
        return "", ""
    m = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
    title = (m.group(1).strip() if m else title_hint.strip() or "会话沉淀")
    if m:
        return title, raw
    md = f"# {title}\n\n{raw}\n"
    return title, md


def _extract_first_image_url(text: str) -> str:
    m = re.search(r"https?://[^\s]+", text, flags=re.IGNORECASE)
    return m.group(0).strip() if m else ""


def _looks_like_image_understand_request(text: str) -> bool:
    t = text.strip().lower()
    if not _extract_first_image_url(t):
        return False
    keys = ["识别", "看图", "读图", "图里", "这张图", "图片内容", "ocr", "提取文字", "描述图片"]
    return any(k in t for k in keys)


def _extract_image_understand_prompt(text: str) -> tuple[str, str]:
    url = _extract_first_image_url(text)
    q = text.replace(url, "").strip(" ：:，,。") if url else text.strip()
    return (q or "请描述这张图并提取关键信息"), url


def _save_session_state(history: list[tuple[str, str]], *, mode: str) -> None:
    try:
        SESSION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": mode,
            "history": [{"q": q, "a": a} for q, a in history[-30:]],
        }
        SESSION_STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_session_state() -> tuple[list[tuple[str, str]], str]:
    if not SESSION_STATE_PATH.exists():
        return [], "auto"
    try:
        data = json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
        rows = data.get("history") or []
        mode = str(data.get("mode", "auto")).strip().lower()
        if mode not in {"auto", "wiki_only", "general_only"}:
            mode = "auto"
        out: list[tuple[str, str]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            q = str(r.get("q", "")).strip()
            a = str(r.get("a", "")).strip()
            if q and a:
                out.append((q, a))
        return out[-30:], mode
    except Exception:
        return [], "auto"


def _clear_session_state_file() -> None:
    try:
        if SESSION_STATE_PATH.exists():
            SESSION_STATE_PATH.unlink()
    except Exception:
        pass


def _replay_session_on_screen(history: list[tuple[str, str]]) -> None:
    if not history:
        return
    console.print("[cyan]—— 已恢复历史对话 ——[/cyan]")
    for q, a in history:
        console.print(f"[black on bright_cyan] You: {q} [/black on bright_cyan]")
        _stream_markdown(a, enabled=False)
    console.print("[cyan]—— 历史对话结束 ——[/cyan]")


def _extract_python_code(text: str) -> str:
    s = text.strip()
    if not s:
        return ""
    # 1. 优先尝试标准 Markdown 提取
    blocks = re.findall(r"```python\s*\n?([\s\S]*?)```", s, flags=re.IGNORECASE)
    if not blocks:
        blocks = re.findall(r"```\s*\n?([\s\S]*?)```", s, flags=re.IGNORECASE)
    
    if blocks:
        code = blocks[0].strip()
        if _is_likely_python(code):
            return code

    # 2. 如果没有标签，检查全文是否就是 Python 代码
    if _is_likely_python(s):
        return s
    
    return ""


def _is_likely_python(text: str) -> bool:
    """判断一段文本是否极大概率为 Python 代码。"""
    t = text.strip()
    if not t:
        return False
    # 常见的 Python 关键字或特征
    features = [
        "import ", "from ", "def ", "class ", "if __name__", 
        "pd.", "os.", "np.", "plt.", "sys.", "print("
    ]
    # 只要命中其中一个且内容较长，或者命中多个
    match_count = sum(1 for f in features if f in t)
    if match_count >= 1 and len(t) > 20:
        return True
    return False


def _looks_like_script_request(text: str) -> bool:
    t = text.lower()
    keys = [
        "python",
        "py脚本",
        "脚本",
        "自动化",
        "批量",
        "合并",
        ".xlsx",
        ".csv",
        "修复bug",
        "debug",
    ]
    return any(k in t for k in keys)


def _extract_existing_py_context(user_text: str, max_files: int = 2) -> str:
    patt = r"([A-Za-z]:\\[^\s\"'<>|?*]+\.py|(?:\.{0,2}[\\/])?[^\s\"'<>|?*]+\.py)"
    found = re.findall(patt, user_text)
    contexts: list[str] = []
    seen: set[str] = set()
    for raw in found:
        p = Path(raw)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        rp = str(p)
        if rp in seen:
            continue
        seen.add(rp)
        if not p.exists() or not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = p.relative_to(Path.cwd()).as_posix() if str(p).startswith(str(Path.cwd())) else str(p)
        contexts.append(f"file: {rel}\n```\\n{content[:12000]}\\n```")
        if len(contexts) >= max_files:
            break
    return "\n\n".join(contexts)


def _extract_path_hints(user_text: str, max_items: int = 4) -> list[str]:
    pats = [
        r"[A-Za-z]:\\[^\s\"'<>|?*]+",
        r"(?:\.{1,2}[\\/])[^\s\"'<>|?*]+",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for pat in pats:
        for m in re.findall(pat, user_text):
            if m in seen:
                continue
            seen.add(m)
            out.append(m)
            if len(out) >= max_items:
                return out
    return out


def _extract_query_symbols(text: str, max_items: int = 16) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text or "")
    stop = {
        "python",
        "please",
        "file",
        "path",
        "true",
        "false",
        "none",
        "class",
        "def",
        "import",
        "from",
        "return",
    }
    out: list[str] = []
    seen: set[str] = set()
    for r in raw:
        k = r.strip()
        if not k or k.lower() in stop:
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
        if len(out) >= max_items:
            break
    return out


def _extract_relevant_snippet(text: str, symbols: list[str], max_chars: int = 2400) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    hits: list[int] = []
    lower_lines = [ln.lower() for ln in lines]
    for sym in symbols:
        s = sym.lower()
        for i, ln in enumerate(lower_lines):
            if f"def {s}(" in ln or f"class {s}" in ln or s in ln:
                hits.append(i)
                break
        if len(hits) >= 3:
            break
    if not hits:
        return "\n".join(lines[: min(len(lines), 80)])[:max_chars]

    ranges: list[tuple[int, int]] = []
    for h in hits:
        ranges.append((max(0, h - 12), min(len(lines), h + 13)))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for st, ed in ranges:
        if not merged or st > merged[-1][1]:
            merged.append((st, ed))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], ed))

    out_lines: list[str] = []
    for st, ed in merged:
        out_lines.extend(lines[st:ed])
        out_lines.append("...")
    out = "\n".join(out_lines).strip()
    return out[:max_chars]


def _build_cross_file_context(
    query: str,
    *,
    exclude_files: set[str] | None = None,
    max_files: int = 3,
) -> str:
    symbols = _extract_query_symbols(query)
    if not symbols:
        return ""

    exclude = {x.replace("\\", "/") for x in (exclude_files or set())}
    root = Path.cwd()
    skip_dirs = {".git", ".wikicoder", "logs", "data", "__pycache__", "wikicoder.egg-info", ".vscode"}
    candidates: list[tuple[int, str, str]] = []  # score, rel, snippet

    for p in root.rglob("*.py"):
        rel = p.relative_to(root).as_posix()
        if rel in exclude:
            continue
        if any(part in skip_dirs for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if not text.strip():
            continue
        low = text.lower()
        score = 0
        for s in symbols:
            ls = s.lower()
            if f"def {ls}(" in low or f"class {ls}" in low:
                score += 3
            if ls in low:
                score += 1
        if score <= 0:
            continue
        snippet = _extract_relevant_snippet(text, symbols)
        if not snippet.strip():
            continue
        candidates.append((score, rel, snippet))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: x[0], reverse=True)
    blocks: list[str] = []
    for score, rel, snippet in candidates[:max_files]:
        blocks.append(f"file: {rel} (score={score})\n```\\n{snippet}\\n```")
    return "\n\n".join(blocks)


def _run_python_script_detailed(script_path: Path, timeout_sec: int = 120, cwd: str | None = None) -> tuple[bool, str, str, int]:
    # 支持自定义工作目录，默认使用 PROJECT_ROOT
    work_dir = cwd or str(PROJECT_ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or "").strip() if isinstance(e.stdout, str) else ""
        err = (e.stderr or "").strip() if isinstance(e.stderr, str) else ""
        detail = f"执行超时：超过 {timeout_sec}s（cwd={work_dir} | script={script_path.name}）"
        if err:
            detail += f"\n{err}"
        return False, out, detail, 124
    except Exception as e:  # noqa: BLE001
        return False, "", f"执行失败（cwd={work_dir} | script={script_path.name}）: {e}", -1
    return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip(), int(proc.returncode)


def _write_script_file(script_path: Path, content: str) -> None:
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(content, encoding="utf-8")


def _run_python_script(script_path: Path, timeout_sec: int = 120, cwd: str | None = None) -> tuple[bool, str]:
    ok, out, err, rc = _run_python_script_detailed(script_path, timeout_sec=timeout_sec, cwd=cwd)
    work_dir = cwd or str(PROJECT_ROOT)
    prefix = f"执行命令: {sys.executable} {script_path.name}\n工作目录: {work_dir}\n"
    if ok:
        msg = prefix + f"脚本执行成功（exit=0）"
        if out:
            msg += f"\n\n标准输出:\n{out}"
        return True, msg
    msg = prefix + f"脚本执行失败（exit={rc})"
    if err:
        msg += f"\n\n错误输出:\n{err}"
    if out:
        msg += f"\n\n标准输出:\n{out}"
    return False, msg


def _classify_script_failure(stderr_text: str, stdout_text: str, exit_code: int) -> tuple[str, str]:
    err = f"{stderr_text}\n{stdout_text}".lower()
    if exit_code == 124 or "执行超时" in err:
        return "timeout", "建议：缩小处理范围、减少输入数据，或把脚本拆分为多阶段执行。"
    if "modulenotfounderror" in err or "no module named" in err:
        return "dependency_missing", "建议：安装缺失依赖后重试（如 pip install <包名>）。"
    if "permissionerror" in err or "access is denied" in err or "拒绝访问" in err:
        return "permission", "建议：检查文件占用/权限，关闭占用程序后重试。"
    if "filenotfounderror" in err or "no such file or directory" in err:
        return "path_not_found", "建议：确认输入路径和文件名是否存在，优先使用绝对路径。"
    if "unicode" in err or "codec" in err or "gbk" in err or "utf-8" in err:
        return "encoding", "建议：统一使用 UTF-8 编码读写，读取时增加 errors='ignore' 兜底。"
    return "runtime_error", "建议：根据报错堆栈定位具体行，优先修复输入校验与异常处理。"


def _extract_missing_modules(err_text: str) -> list[str]:
    text = err_text or ""
    names = re.findall(r"No module named ['\"]([A-Za-z0-9_.-]+)['\"]", text)
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def _normalize_pip_package(mod_name: str) -> str:
    m = (mod_name or "").strip()
    mapping = {
        "cv2": "opencv-python",
        "sklearn": "scikit-learn",
        "pil": "Pillow",
        "yaml": "PyYAML",
        "docx": "python-docx",
    }
    low = m.lower()
    return mapping.get(low, m)


def _install_python_packages(packages: list[str], timeout_sec: int = 180) -> tuple[bool, str]:
    if not packages:
        return True, "无缺失依赖需要安装。"
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout_sec,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"依赖安装失败：{e}"
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode == 0:
        msg = "依赖安装成功。"
        if out:
            msg += f"\n\n安装输出:\n{out[-2000:]}"
        return True, msg
    msg = f"依赖安装失败（exit={proc.returncode}）"
    if err:
        msg += f"\n\n错误输出:\n{err[-2000:]}"
    if out:
        msg += f"\n\n标准输出:\n{out[-2000:]}"
    return False, msg


def _extract_probe_json(stdout_text: str) -> str:
    marker = "WIKICODER_PROBE_JSON="
    for line in stdout_text.splitlines():
        if line.startswith(marker):
            return line[len(marker) :].strip()
    return ""


def _extract_marker_json(stdout_text: str, marker: str) -> str:
    for line in (stdout_text or "").splitlines():
        if line.startswith(marker):
            return line[len(marker) :].strip()
    return ""


def _extract_excel_constraints(user_text: str) -> dict[str, object]:
    text = user_text or ""
    out: dict[str, object] = {}
    m = re.search(r"第\s*(\d+)\s*行", text)
    if m:
        try:
            human_row = int(m.group(1))
            if human_row >= 1:
                out["header_row_human"] = human_row
                out["header_index_zero_based"] = human_row - 1
        except Exception:
            pass
    if "第二行" in text and ("表头" in text or "标题" in text or "header" in text.lower()):
        out["header_row_human"] = 2
        out["header_index_zero_based"] = 1
    if any(k in text.lower() for k in [".xlsx", "excel", "openpyxl", "sheet", "合并"]):
        out["excel_task"] = True
    return out


def _verify_excel_result_quality(stdout_text: str) -> tuple[bool, str]:
    raw = _extract_marker_json(stdout_text, "WIKICODER_RESULT_JSON=")
    if not raw:
        return False, "缺少结果质量报告（未输出 WIKICODER_RESULT_JSON）。"
    # 先尝试 JSON 解析，失败后兼容 Python dict 字面量（单引号等）
    data: dict = {}
    try:
        data = json.loads(raw)
    except Exception:
        try:
            data = ast.literal_eval(raw)
            if not isinstance(data, dict):
                return False, "结果质量报告格式异常（非 dict）。"
        except Exception:
            return False, "结果质量报告不是合法 JSON 或 Python dict。"

    row_count = int(data.get("row_count", 0) or 0)
    col_count = int(data.get("col_count", 0) or 0)
    nan_ratio = float(data.get("nan_ratio", 0) or 0)
    out_file = str(data.get("output_file", "")).strip()

    if not out_file:
        return False, "结果质量报告缺少 output_file。"
    if row_count <= 0:
        return False, "合并结果行数为 0。"
    if col_count <= 0:
        return False, "合并结果列数为 0。"
    if col_count > 300:
        return False, f"列数异常偏大（{col_count}），疑似表头错位。"
    if nan_ratio >= 0.50:
        return False, f"空值占比过高（{nan_ratio:.2%}），疑似列对齐失败或混入了非源文件。"
    return True, f"质量校验通过：rows={row_count}, cols={col_count}, nan_ratio={nan_ratio:.2%}"


def _confirm_local_operation(consent_state: dict[str, str], action_desc: str) -> bool:
    mode = consent_state.get("mode", "ask")
    if mode == "all":
        return True
    if mode == "deny":
        return False

    console.print(
        f"[yellow]即将执行本地操作：{action_desc}[/yellow]\n"
        "[cyan]请选择：[/cyan] [green]y[/green]=同意本次  "
        "[green]a[/green]=同意本次会话所有操作  "
        "[red]n[/red]=不同意"
    )
    ans = input("授权(y/a/n): ").strip().lower()
    if ans == "a":
        consent_state["mode"] = "all"
        return True
    if ans == "y":
        return True
    return False


def _extract_target_cwd(path_hints: list[str]) -> str | None:
    """从路径线索中提取用户的目标工作目录。"""
    for hint in path_hints:
        p = Path(hint.strip())
        if p.is_dir():
            return str(p.resolve())
        if p.is_file():
            return str(p.parent.resolve())
        # 尝试作目录解析（可能用户给的路径尚不存在但父目录存在）
        if p.parent.is_dir():
            return str(p.parent.resolve())
    return None


def _get_scripts_dir() -> Path:
    """获取脚本临时目录（.wikicoder/scripts/），自动创建。"""
    scripts_dir = PROJECT_ROOT / ".wikicoder" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


def _cleanup_old_scripts(max_age_days: int = 7) -> None:
    """清理超过指定天数的旧脚本文件。"""
    scripts_dir = PROJECT_ROOT / ".wikicoder" / "scripts"
    if not scripts_dir.exists():
        return
    import time as _time
    cutoff = _time.time() - max_age_days * 86400
    for f in scripts_dir.glob("wikicoder_*.py"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except Exception:
            pass


def _auto_script_pipeline(
    *,
    agent: WikiFirstAgent,
    user_query: str,
    resp: AgentResponse,
    history: list[tuple[str, str]],
    consent_state: dict[str, str],
) -> AgentResponse:
    try:
        return _auto_script_pipeline_inner(
            agent=agent,
            user_query=user_query,
            resp=resp,
            history=history,
            consent_state=consent_state,
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]编码流水线异常：{e}[/red]")
        return AgentResponse(
            thought="code-pipeline:error",
            actions=resp.actions + [f"pipeline_error:{type(e).__name__}"],
            output=(
                f"{resp.output}\n\n---\n"
                f"[编码流水线异常]\n{type(e).__name__}: {e}\n\n"
                "可能原因：LLM 超时、网络中断、或模型返回异常。\n"
                "建议：1) 检查网络连接 2) 增大 config.yaml 中 timeout_seconds（建议 120+）3) 重试"
            ),
        )


def _auto_script_pipeline_inner(
    *,
    agent: WikiFirstAgent,
    user_query: str,
    resp: AgentResponse,
    history: list[tuple[str, str]],
    consent_state: dict[str, str],
) -> AgentResponse:
    # 清理旧脚本文件
    _cleanup_old_scripts()

    excel_constraints = _extract_excel_constraints(user_query)
    is_excel_task = bool(excel_constraints.get("excel_task"))
    path_hints = _extract_path_hints(user_query)
    hint_text = ", ".join(path_hints) if path_hints else "(未显式给出路径，默认当前目录)"
    # 从路径线索提取目标工作目录
    target_cwd = _extract_target_cwd(path_hints)
    cwd_info = f"脚本工作目录: {target_cwd}" if target_cwd else "脚本工作目录: 项目根目录"

    console.print("[dim]编码流程: 1/3 数据结构探测[/dim]")
    probe_prompt = (
        "请生成一个只读的 Python 探测脚本，用于分析用户需求涉及的数据结构。\n"
        "要求：\n"
        "1) 检测操作系统类型、版本及包管理器（如 apt, yum, brew）\n"
        "2) 探测涉及的路径或文件是否存在，若是表格文件请报告结构（行列、空值等）\n"
        "3) 最后在 stdout 输出一行：WIKICODER_PROBE_JSON=<json>（使用 json.dumps 输出）\n"
        "4) 仅输出 Python 代码，不要解释\n"
        "7) 脚本必须包含 if __name__ == '__main__': 入口\n\n"
        f"用户需求：{user_query}\n"
        f"路径线索：{hint_text}\n"
        f"{cwd_info}\n"
        f"硬约束：{json.dumps(excel_constraints, ensure_ascii=False)}"
    )
    probe_resp = _run_agent_with_thinking(
        agent,
        user_input=probe_prompt,
        force_wiki=False,
        mode="general_only",
        history=history,
        silent=True,
    )
    probe_code = _extract_python_code(probe_resp.output)
    if not probe_code:
        return resp

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 动态决定脚本存放位置：优先放在数据同级目录
    if target_cwd and Path(target_cwd).is_dir():
        scripts_dir = Path(target_cwd)
    else:
        scripts_dir = _get_scripts_dir()
        
    probe_name = f"wikicoder_probe_{ts}.py"
    probe_path = scripts_dir / probe_name
    if not _confirm_local_operation(consent_state, f"写入探测脚本 {probe_name} 并执行（只读探测）"):
        return AgentResponse(
            thought=resp.thought,
            actions=resp.actions + ["local-op:denied-by-user"],
            output=f"{resp.output}\n\n---\n[本地操作]\n用户拒绝执行探测，未进入自动化实现。",
        )
    _write_script_file(probe_path, probe_code)
    ok_probe, probe_out, probe_err, probe_rc = _run_python_script_detailed(probe_path, cwd=target_cwd)
    probe_json = _extract_probe_json(probe_out)
    probe_summary = probe_json if probe_json else json.dumps(
        {"stdout": probe_out[:2000], "stderr": probe_err[:2000], "exit": probe_rc}, ensure_ascii=False
    )
    probe_status = "成功" if ok_probe else "失败（继续按已有信息尝试）"

    console.print("[dim]编码流程: 2/3 生成执行脚本[/dim]")
    # 构建标准化的脚本生成 Prompt
    script_prompt = (
        "你将根据探测结果实现自动化脚本。请仅输出完整 Python 代码，不要解释。\n\n"
        "=== 代码规范（必须遵守） ===\n"
        "1) 脚本必须包含 if __name__ == '__main__': 入口\n"
        "2) 所有文件读写使用 encoding='utf-8', errors='ignore'\n"
        "3) 中文路径使用 raw string 或 os.path.join，不要手动拼接反斜杠\n"
        "4) 输出文件名必须唯一（建议加时间戳），避免覆盖源文件或已存在文件\n"
        "5) 写文件前检查目标路径是否被占用（try/except PermissionError）\n"
        "6) 对输入异常做健壮处理（文件不存在、格式异常、空数据等）\n"
        "7) 打印关键进度和最终结果\n"
        "8) 可用依赖：pandas, openpyxl, os, sys, json, glob, pathlib, re, datetime, subprocess\n\n"
        "=== 专项规范 ===\n"
        "9) 若涉及系统操作（如 UOS/Linux 安装软件），请使用 subprocess.run 调用系统命令\n"
        "10) 优先处理权限问题，若需 sudo 请确保逻辑闭环\n"
        "11) 若是表格合并，先对列名执行 strip() 去除空格\n"
        "12) 优先使用 pandas + engine='openpyxl'\n"
        "13) 合并时必须以第一个源文件的列顺序为基准，后续文件按此顺序对齐\n"
        "14) 所有路径必须使用绝对路径，不要使用相对路径\n"
        "14) 【严重警告：禁止手动过滤行】pandas.read_excel 默认会将第 0 行作为标题。读取后，DataFrame 的第一行即为有效数据。\n"
        "    因此，在 concat 合并后续文件时，绝对禁止使用 .iloc[1:] 或任何手动跳过第一行的操作。\n"
        "    正确的逻辑应该是：直接 pd.concat([df1, df2, ...])，pandas 会自动处理列对齐。\n"
        "15) 【数据对齐校验】生成的代码必须在合并前后打印行数。合并后总行数应等于各分表行数之和。\n"
        "16) 【编码安全】脚本开头的 print 语句前，请务必执行：\n"
        "    import sys, io; sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')\n"
        "    严禁在 print 中使用 Emoji（如 ✅、❌）或特殊符号，仅使用标准 ASCII 字符。\n"
        "17) 若为 Excel 任务，最终输出一行：\n"
        "    print('WIKICODER_RESULT_JSON=' + json.dumps({...}))\n"
        "    其中 JSON 包含 output_file, row_count, col_count, nan_ratio\n"
        "    【必须用 json.dumps 生成，不要用 str() 或 f-string】\n\n"
        f"=== 任务信息 ===\n"
        f"用户需求：{user_query}\n"
        f"路径线索：{hint_text}\n"
        f"{cwd_info}\n"
        f"探测状态：{probe_status}\n"
        f"硬约束：{json.dumps(excel_constraints, ensure_ascii=False)}\n"
        f"探测结果(JSON)：\n{probe_summary[:12000]}"
    )
    script_resp = _run_agent_with_thinking(
        agent,
        user_input=script_prompt,
        force_wiki=False,
        mode="general_only",
        history=history,
        silent=True,
    )
    code = _extract_python_code(script_resp.output)
    if not code:
        return AgentResponse(
            thought=resp.thought,
            actions=resp.actions + [f"write_file({probe_name})", f"run_python({probe_name})", "gen_script:failed"],
            output=f"{resp.output}\n\n---\n[探测]\n{probe_status}\n\n[自动化脚本生成]\n模型未返回可执行 Python 代码。",
        )

    script_name = f"wikicoder_task_{ts}.py"
    # 业务脚本同样跟随 scripts_dir
    script_path = scripts_dir / script_name
    if not _confirm_local_operation(consent_state, f"写入业务脚本 {script_name} 并执行"):
        return AgentResponse(
            thought=resp.thought,
            actions=resp.actions + [f"write_file({probe_name})", f"run_python({probe_name})", "local-op:denied-by-user"],
            output=f"{resp.output}\n\n---\n[探测]\n{probe_status}\n\n[本地操作]\n用户拒绝写入/执行业务脚本。",
        )
    _write_script_file(script_path, code)
    console.print(f"[green]已生成脚本：{script_path}[/green]")

    actions = resp.actions + [
        f"write_file({probe_name})",
        f"run_python({probe_name})",
        f"write_file({script_name})",
        f"run_python({script_name})",
    ]

    console.print("[dim]编码流程: 3/3 运行与自动修复[/dim]")
    ok, run_msg = _run_python_script(script_path, cwd=target_cwd)
    if ok and is_excel_task:
        q_ok, q_msg = _verify_excel_result_quality(run_msg)
        run_msg += f"\n\n[语义校验]\n{q_msg}"
        if not q_ok:
            ok = False
    if not ok:
        category, advice = _classify_script_failure(run_msg, "", 1)
        run_msg = f"{run_msg}\n\n故障分类: {category}\n{advice}"
        missing = _extract_missing_modules(run_msg)
        if missing:
            pkgs = [_normalize_pip_package(x) for x in missing]
            pkgs = list(dict.fromkeys([p for p in pkgs if p.strip()]))
            if _confirm_local_operation(consent_state, f"安装缺失依赖并重试：{', '.join(pkgs)}"):
                console.print(f"[yellow]检测到缺失依赖，准备安装：{', '.join(pkgs)}[/yellow]")
                ok_i, msg_i = _install_python_packages(pkgs)
                run_msg += f"\n\n[依赖安装]\n{msg_i}"
                actions.append(f"pip_install({','.join(pkgs)})")
                if ok_i:
                    ok_retry, retry_msg = _run_python_script(script_path, cwd=target_cwd)
                    actions.append(f"run_python({script_name}):retry_after_pip")
                    run_msg += f"\n\n[安装后重试]\n{retry_msg}"
                    if ok_retry:
                        return AgentResponse(
                            thought=resp.thought,
                            actions=actions,
                            output=f"{resp.output}\n\n---\n[探测状态]\n{probe_status}\n\n[自动执行结果]\n{run_msg}",
                        )
    all_msgs = [
        f"[探测状态]\n{probe_status}",
        f"[自动执行结果]\n{run_msg}",
    ]
    if ok:
        return AgentResponse(
            thought=resp.thought,
            actions=actions,
            output=f"{resp.output}\n\n---\n" + "\n\n".join(all_msgs),
        )

    # === 自动修复循环（上限10轮 + 智能退出） ===
    MAX_FIX_ATTEMPTS = 10
    current_code = code
    attempt = 1
    last_category = ""
    same_category_count = 0
    while True:
        # 渐进式策略：前2轮修复、第3-5轮重写、第6轮起强制重读需求
        if attempt <= 2:
            fix_strategy = (
                "请修复以下脚本中的错误。仅输出完整 Python 代码，不要解释。\n"
                "重点关注报错堆栈中的具体行号和错误类型。"
            )
        elif attempt <= 5:
            fix_strategy = (
                "之前的修复尝试未能解决问题。请抛弃原有思路，从零重写整个脚本。\n"
                "请特别注意文件读写编码、路径规范及异常处理。"
            )
        else:
            fix_strategy = (
                "【深度重构模式】多次修复仍未成功。请彻底重新审视用户需求和探测结果，\n"
                "从最基础的逻辑开始检查。仅输出完整代码。"
            )

        # 注入前轮失败原因摘要
        prev_failures = ""
        if attempt > 1:
            recent_fail_msgs = [m for m in all_msgs if m.startswith("[第")]
            if recent_fail_msgs:
                prev_failures = (
                    "\n\n=== 前轮修复失败摘要（请避免重复犯同样错误） ===\n"
                    + "\n".join(m[:500] for m in recent_fail_msgs[-2:])
                )

        fix_prompt = (
            f"{fix_strategy}\n\n"
            f"用户原始需求：{user_query}\n"
            f"脚本文件名：{script_name}\n"
            f"修复轮次：{attempt}/{MAX_FIX_ATTEMPTS}\n"
            f"{cwd_info}\n"
            f"硬约束：{json.dumps(excel_constraints, ensure_ascii=False)}\n"
            f"探测结果(JSON)：\n{probe_summary[:10000]}\n\n"
            "当前脚本：\n"
            f"```python\n{current_code}\n```\n\n"
            f"最近报错：\n```\n{all_msgs[-1][:7000]}\n```"
            f"{prev_failures}"
        )
        fix_resp = _run_agent_with_thinking(
            agent,
            user_input=fix_prompt,
            force_wiki=False,
            mode="general_only",
            history=history,
            silent=True,
        )
        fix_code = _extract_python_code(fix_resp.output)
        if not fix_code:
            all_msgs.append(f"[第{attempt}轮自动修复] 模型未返回可执行代码。")
            break

        if not _confirm_local_operation(consent_state, f"覆盖脚本 {script_name} 并再次执行（第{attempt}轮修复）"):
            all_msgs.append(f"[第{attempt}轮自动修复] 用户拒绝继续本地写入/执行。")
            actions.append(f"auto_fix:{attempt}:denied")
            break

        _write_script_file(script_path, fix_code)
        current_code = fix_code
        ok_i, run_msg_i = _run_python_script(script_path, cwd=target_cwd)
        if ok_i and is_excel_task:
            q_ok_i, q_msg_i = _verify_excel_result_quality(run_msg_i)
            run_msg_i += f"\n\n[语义校验]\n{q_msg_i}"
            if not q_ok_i:
                ok_i = False
        if not ok_i:
            category_i, advice_i = _classify_script_failure(run_msg_i, "", 1)
            run_msg_i = f"{run_msg_i}\n\n故障分类: {category_i}\n{advice_i}"
            # 智能退出：连续 3 次相同故障分类（且非超时/依赖问题）则终止
            if category_i == last_category and category_i not in {"timeout", "dependency_missing"}:
                same_category_count += 1
            else:
                same_category_count = 0
                last_category = category_i
            if same_category_count >= 3:
                all_msgs.append(f"[第{attempt}轮自动修复执行结果]\n{run_msg_i}")
                all_msgs.append(
                    f"[自动修复状态] 连续 {same_category_count + 1} 轮同类故障（{category_i}），"
                    "自动修复无法解决，建议人工介入。"
                )
                actions.extend([f"auto_fix:{attempt}", f"run_python({script_name})"])
                break

        # 注入前轮失败原因摘要
        prev_failures = ""
        if attempt > 1:
            recent_fail_msgs = [m for m in all_msgs if m.startswith("[第")]
            if recent_fail_msgs:
                prev_failures = (
                    "\n\n=== 前轮修复失败摘要（请避免重复犯同样错误） ===\n"
                    + "\n".join(m[:500] for m in recent_fail_msgs[-2:])
                )

        fix_prompt = (
            f"{fix_strategy}\n\n"
            f"用户原始需求：{user_query}\n"
            f"脚本文件名：{script_name}\n"
            f"修复轮次：{attempt}/{MAX_FIX_ATTEMPTS}\n"
            f"{cwd_info}\n"
            f"硬约束：{json.dumps(excel_constraints, ensure_ascii=False)}\n"
            f"探测结果(JSON)：\n{probe_summary[:10000]}\n\n"
            "当前脚本：\n"
            f"```python\n{current_code}\n```\n\n"
            f"最近报错：\n```\n{all_msgs[-1][:7000]}\n```"
            f"{prev_failures}"
        )
        fix_resp = _run_agent_with_thinking(
            agent,
            user_input=fix_prompt,
            force_wiki=False,
            mode="general_only",
            history=history,
            silent=True,
        )
        fix_code = _extract_python_code(fix_resp.output)
        if not fix_code:
            all_msgs.append(f"[第{attempt}轮自动修复] 模型未返回可执行代码。")
            break

        if not _confirm_local_operation(consent_state, f"覆盖脚本 {script_name} 并再次执行（第{attempt}轮修复）"):
            all_msgs.append(f"[第{attempt}轮自动修复] 用户拒绝继续本地写入/执行。")
            actions.append(f"auto_fix:{attempt}:denied")
            break

        _write_script_file(script_path, fix_code)
        current_code = fix_code
        ok_i, run_msg_i = _run_python_script(script_path, cwd=target_cwd)
        if ok_i and is_excel_task:
            q_ok_i, q_msg_i = _verify_excel_result_quality(run_msg_i)
            run_msg_i += f"\n\n[语义校验]\n{q_msg_i}"
            if not q_ok_i:
                ok_i = False
        if not ok_i:
            category_i, advice_i = _classify_script_failure(run_msg_i, "", 1)
            run_msg_i = f"{run_msg_i}\n\n故障分类: {category_i}\n{advice_i}"
            # 智能退出：连续相同故障分类则终止
            if category_i == last_category:
                same_category_count += 1
            else:
                same_category_count = 0
                last_category = category_i
            if same_category_count >= 2:
                all_msgs.append(f"[第{attempt}轮自动修复执行结果]\n{run_msg_i}")
                all_msgs.append(
                    f"[自动修复状态] 连续 {same_category_count + 1} 轮同类故障（{category_i}），"
                    "自动修复无法解决，建议人工介入。"
                )
                actions.extend([f"auto_fix:{attempt}", f"run_python({script_name})"])
                break
            missing_i = _extract_missing_modules(run_msg_i)
            if missing_i:
                pkgs_i = [_normalize_pip_package(x) for x in missing_i]
                pkgs_i = list(dict.fromkeys([p for p in pkgs_i if p.strip()]))
                if _confirm_local_operation(consent_state, f"安装缺失依赖并重试：{', '.join(pkgs_i)}"):
                    console.print(f"[yellow]检测到缺失依赖，准备安装：{', '.join(pkgs_i)}[/yellow]")
                    ok_p, msg_p = _install_python_packages(pkgs_i)
                    actions.append(f"pip_install({','.join(pkgs_i)})")
                    run_msg_i += f"\n\n[依赖安装]\n{msg_p}"
                    if ok_p:
                        ok_retry_i, retry_msg_i = _run_python_script(script_path, cwd=target_cwd)
                        actions.append(f"run_python({script_name}):retry_after_pip")
                        run_msg_i += f"\n\n[安装后重试]\n{retry_msg_i}"
                        if ok_retry_i:
                            all_msgs.append(f"[第{attempt}轮自动修复执行结果]\n{run_msg_i}")
                            all_msgs.append(f"[自动修复状态] 已在第{attempt}轮通过安装依赖修复成功。")
                            return AgentResponse(
                                thought=resp.thought,
                                actions=actions,
                                output=f"{resp.output}\n\n---\n" + "\n\n".join(all_msgs),
                            )
        actions.extend([f"auto_fix:{attempt}", f"run_python({script_name})"])
        all_msgs.append(f"[第{attempt}轮自动修复执行结果]\n{run_msg_i}")
        if ok_i:
            all_msgs.append(f"[自动修复状态] 已在第{attempt}轮修复成功。")
            return AgentResponse(
                thought=resp.thought,
                actions=actions,
                output=f"{resp.output}\n\n---\n" + "\n\n".join(all_msgs),
            )
        attempt += 1
        if attempt > MAX_FIX_ATTEMPTS:
            all_msgs.append(f"[自动修复状态] 已达到安全上限({MAX_FIX_ATTEMPTS}轮)，仍未成功，建议人工检查报错信息。")
            break

    return AgentResponse(
        thought=resp.thought,
        actions=actions,
        output=f"{resp.output}\n\n---\n" + "\n\n".join(all_msgs),
    )



def _stream_markdown(text: str, enabled: bool = True, delay: float = 0.006) -> None:
    if not enabled:
        console.print(Markdown(text))
        return

    current = ""
    with Live(Markdown(""), console=console, refresh_per_second=20) as live:
        for ch in text:
            current += ch
            live.update(Markdown(current))
            if delay > 0:
                time.sleep(delay)



def run_sync() -> dict[str, int]:
    config = load_config()
    atomizer = Atomizer(config)
    return atomizer.sync()



def build_agent(config: AppConfig | None = None) -> WikiFirstAgent:
    return WikiFirstAgent(config or load_config())


def build_llm(config: AppConfig | None = None) -> LLMClient:
    cfg = config or load_config()
    return LLMClient(cfg.llm)



def _run_agent_with_thinking(
    agent: WikiFirstAgent,
    *,
    user_input: str,
    force_wiki: bool,
    mode: str = "auto",
    code_context: str = "",
    response_mode: str = "answer",
    target_file: str = "",
    history: list[tuple[str, str]] | None = None,
    silent: bool = False,
):
    state: dict[str, object] = {}
    token_q: "queue.Queue[str]" = queue.Queue()
    status_q: "queue.Queue[str]" = queue.Queue()
    seen_status: set[str] = set()

    def _work() -> None:
        try:
            state["resp"] = agent.run(
                user_input,
                force_wiki=force_wiki,
                mode=mode,  # type: ignore[arg-type]
                code_context=code_context,
                response_mode=response_mode,  # type: ignore[arg-type]
                target_file=target_file,
                history=history,
                on_token=lambda s: token_q.put(s),
                on_status=lambda s: status_q.put(s),
            )
        except Exception as exc:  # noqa: BLE001
            state["err"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()

    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    idx = 0
    start = time.perf_counter()
    streamed = False

    term_state = _enable_posix_cbreak_if_needed()
    try:
        with Live("", console=console, refresh_per_second=12, transient=True) as live:
            while t.is_alive() or (not token_q.empty()):
                while True:
                    try:
                        st = status_q.get_nowait()
                    except Exception:
                        break
                    if st in seen_status:
                        continue
                    seen_status.add(st)
                    console.print(f"[dim]step: {st}[/dim]")

                chunks: list[str] = []
                while True:
                    try:
                        chunks.append(token_q.get_nowait())
                    except Exception:
                        break
                if chunks:
                    if not silent:
                        if not streamed:
                            streamed = True
                            live.stop()
                        console.print("".join(chunks), end="")
                    else:
                        streamed = True # 在静默模式下仅标记已开始，但不停止动画

                elapsed = time.perf_counter() - start
                phase = "检索 Wiki + 调用模型"
                if mode == "general_only":
                    phase = "调用通用模型"
                elif mode == "wiki_only":
                    phase = "仅检索 Wiki"
                
                if not live.is_started: # 如果被非静默模式停掉了，就不更新了
                    continue

                status_suffix = ""
                if silent and streamed:
                    # 尝试计算已接收内容的大致大小
                    received_size = token_q.qsize() * 0.5 # 估算
                    status_suffix = f" [正在接收数据...]"

                live.update(
                    f"[bold cyan]{frames[idx % len(frames)]} {phase} {elapsed:.1f}s[/bold cyan] "
                    f"{status_suffix} "
                    f"[dim]（按 ESC 取消；Windows 可用 Ctrl+C）[/dim]"
                )
                idx += 1

                if _escape_pressed():
                    return AgentResponse(
                        thought="cancelled-by-user",
                        actions=["cancelled: ESC pressed"],
                        output="已取消本次提问。",
                    )
                time.sleep(0.1)
    finally:
        _restore_posix_terminal(term_state)

    if streamed:
        console.print()

    if "err" in state:
        raise state["err"]  # type: ignore[misc]
    resp = state["resp"]  # type: ignore[assignment]
    try:
        setattr(resp, "_already_streamed", streamed)
    except Exception:
        pass
    return resp  # type: ignore[return-value]


def _run_llm_with_thinking(llm: LLMClient, *, system_prompt: str, user_prompt: str, phase: str = "整理中") -> str:
    state: dict[str, object] = {}

    def _work() -> None:
        try:
            state["text"] = llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
        except Exception as exc:  # noqa: BLE001
            state["err"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()

    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    idx = 0
    start = time.perf_counter()
    cancelled = False

    term_state = _enable_posix_cbreak_if_needed()
    try:
        with Live("", console=console, refresh_per_second=12, transient=True) as live:
            while t.is_alive():
                elapsed = time.perf_counter() - start
                live.update(
                    f"[bold cyan]{frames[idx % len(frames)]} {phase} {elapsed:.1f}s[/bold cyan] "
                    "[dim]（按 ESC 取消；Windows 可用 Ctrl+C）[/dim]"
                )
                idx += 1
                if _escape_pressed():
                    cancelled = True
                    break
                time.sleep(0.1)
    finally:
        _restore_posix_terminal(term_state)

    if cancelled:
        return ""
    if "err" in state:
        raise state["err"]  # type: ignore[misc]
    return str(state.get("text", "")).strip()


def _run_image_generate_with_thinking(
    llm: LLMClient,
    *,
    prompt: str,
    size: str = "1024x1024",
    phase: str = "图片生成中",
) -> str:
    state: dict[str, object] = {}

    def _work() -> None:
        try:
            state["text"] = llm.image_generate(prompt=prompt, size=size)
        except Exception as exc:  # noqa: BLE001
            state["err"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()

    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    idx = 0
    start = time.perf_counter()
    cancelled = False

    term_state = _enable_posix_cbreak_if_needed()
    try:
        with Live("", console=console, refresh_per_second=12, transient=True) as live:
            while t.is_alive():
                elapsed = time.perf_counter() - start
                live.update(
                    f"[bold cyan]{frames[idx % len(frames)]} {phase} {elapsed:.1f}s[/bold cyan] "
                    "[dim]（按 ESC 取消；Windows 可用 Ctrl+C）[/dim]"
                )
                idx += 1
                if _escape_pressed():
                    cancelled = True
                    break
                time.sleep(0.1)
    finally:
        _restore_posix_terminal(term_state)

    if cancelled:
        return ""
    if "err" in state:
        raise state["err"]  # type: ignore[misc]
    return str(state.get("text", "")).strip()


def _run_image_understand_with_thinking(
    llm: LLMClient,
    *,
    prompt: str,
    image_url: str,
    phase: str = "图片理解中",
) -> str:
    state: dict[str, object] = {}

    def _work() -> None:
        try:
            state["text"] = llm.image_understand(prompt=prompt, image_url=image_url)
        except Exception as exc:  # noqa: BLE001
            state["err"] = exc

    t = threading.Thread(target=_work, daemon=True)
    t.start()

    frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    idx = 0
    start = time.perf_counter()
    cancelled = False

    term_state = _enable_posix_cbreak_if_needed()
    try:
        with Live("", console=console, refresh_per_second=12, transient=True) as live:
            while t.is_alive():
                elapsed = time.perf_counter() - start
                live.update(
                    f"[bold cyan]{frames[idx % len(frames)]} {phase} {elapsed:.1f}s[/bold cyan] "
                    "[dim]（按 ESC 取消；Windows 可用 Ctrl+C）[/dim]"
                )
                idx += 1
                if _escape_pressed():
                    cancelled = True
                    break
                time.sleep(0.1)
    finally:
        _restore_posix_terminal(term_state)

    if cancelled:
        return ""
    if "err" in state:
        raise state["err"]  # type: ignore[misc]
    return str(state.get("text", "")).strip()


def _print_trace(resp_thought: str, resp_actions: list[str]) -> None:
    console.print(f"[dim]thought:[/dim] {resp_thought}")
    for a in resp_actions:
        console.print(f"[dim]- {a}[/dim]")


def _print_patch_preview(patch_text: str) -> None:
    items = summarize_unified_diff(patch_text)
    if not items:
        console.print("[yellow]No parseable unified diff found in output.[/yellow]")
        return
    console.print("[cyan]Patch preview:[/cyan]")
    for it in items:
        console.print(f"- {it.file or '(unknown)'} | hunks={it.hunks} +{it.added} -{it.removed}")



def _set_vault_path(path_str: str) -> tuple[bool, str]:
    path_str = path_str.strip()
    if not path_str:
        return False, "路径不能为空。"
    cfg_path = DEFAULT_CONFIG_PATH
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    ws = data.get("wiki_strategy") or {}
    if not isinstance(ws, dict):
        ws = {}
    ws["vault_path"] = path_str
    ws.setdefault("raw_dir", "raw")
    ws.setdefault("wiki_dir", "wiki")
    ws.setdefault("processed_dir", "wiki_processed")
    ws.setdefault("raw_subdirs", ["inbox", "drafts", "archive"])
    ws.setdefault("wiki_subdirs", ["entities", "concepts", "comparisons", "queries"])
    # clear explicit path overrides so vault auto-rules take effect
    ws.pop("raw_path", None)
    ws.pop("wiki_path", None)
    ws.pop("processed_path", None)
    data["wiki_strategy"] = ws
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return True, f"已更新 vault_path 为: {path_str}（raw/wiki/processed 将自动在该目录下构建）"


def _set_model_config(model_cmd: str) -> tuple[bool, str]:
    cmd = model_cmd.strip()
    cfg_path = DEFAULT_CONFIG_PATH
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict):
        data = {}
    llm = data.get("llm") or {}
    if not isinstance(llm, dict):
        llm = {}
    key = cmd.lower()
    if key in {"jiutian-think-v3", "think"}:
        llm["provider"] = "jiutian"
        llm["model"] = "jiutian-think-v3"
        msg = "已切换文本模型为：jiutian-think-v3（思考模型）"
    elif key in {"jiutian-lan-comv3", "chat", "dialog"}:
        llm["provider"] = "jiutian"
        llm["model"] = "jiutian-lan-comv3"
        msg = "已切换文本模型为：jiutian-lan-comv3（对话模型）"
    else:
        return False, (
            "用法：/model <name>\n"
            "可选：jiutian-think-v3 | jiutian-lan-comv3\n"
            "别名：think/chat\n"
            "说明：图片理解/图片生成模型会根据问题自动切换。"
        )

    data["llm"] = llm
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return True, msg


def _print_runtime_settings(config: AppConfig, *, session_mode: str) -> None:
    llm = config.llm
    ws = config.wiki_strategy
    console.print(
        "[dim]"
        f"provider={llm.provider} | text_model={llm.model} | session_mode={session_mode}\n"
        f"img2text_model={llm.image_understand_model or '-'} | imggen_model={llm.image_generate_model or '-'}\n"
        f"raw={ws.raw_path}\nwiki={ws.wiki_path}\nprocessed={ws.processed_path}"
        "[/dim]"
    )


def _ensure_auto_image_models(config: AppConfig) -> None:
    # 自动兜底：不要求用户手动设置图片模型
    if config.llm.provider.strip().lower() != "jiutian":
        return
    if not (config.llm.image_understand_model or "").strip():
        config.llm.image_understand_model = "LLMImage2Text"
    if not (config.llm.image_understand_url or "").strip():
        config.llm.image_understand_url = LLMClient.JIUTIAN_IMAGE_UNDERSTAND_URL
    if not (config.llm.image_generate_model or "").strip():
        config.llm.image_generate_model = "cntxt2image"
    if not (config.llm.image_generate_url or "").strip():
        config.llm.image_generate_url = LLMClient.JIUTIAN_IMAGE_GENERATE_URL
    if not (config.llm.image_asset_host or "").strip():
        config.llm.image_asset_host = "https://jiutian.10086.cn"


def _extract_image_fields(obj: object) -> tuple[list[str], list[str], list[str]]:
    urls: list[str] = []
    b64s: list[str] = []
    texts: list[str] = []

    def walk(x: object) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if isinstance(v, str):
                    if lk in {"url", "image_url"} and v.strip():
                        urls.append(v.strip())
                    elif "base64" in lk or lk in {"b64_json", "image"}:
                        # simple heuristic: long base64-like string
                        if len(v) > 100 and all(ch.isalnum() or ch in "+/=\n\r" for ch in v[:200]):
                            b64s.append(v.strip())
                    elif lk in {"text", "content", "message"} and len(v.strip()) > 0:
                        texts.append(v.strip())
                else:
                    walk(v)
        elif isinstance(x, list):
            for item in x:
                walk(item)

    walk(obj)
    # de-dup
    urls = list(dict.fromkeys(urls))
    b64s = list(dict.fromkeys(b64s))
    texts = list(dict.fromkeys(texts))
    return urls, b64s, texts


def _normalize_image_url(url: str, image_asset_host: str | None = None) -> str:
    u = (url or "").strip()
    if not u:
        return u
    if u.startswith("http://") or u.startswith("https://"):
        return u
    host = (image_asset_host or "https://jiutian.10086.cn").strip().rstrip("/")
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"https://{host}"
    if u.startswith("/"):
        return f"{host}{u}"
    return f"{host}/{u}"


def _save_image_result(
    raw_result: str,
    save_dir: str,
    prefix: str,
    image_asset_host: str | None = None,
) -> tuple[list[str], list[str], str]:
    out_dir = Path(save_dir)
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta_path = out_dir / f"{prefix}_{ts}.json"
    meta_path.write_text(raw_result, encoding="utf-8")

    urls: list[str] = []
    saved_files: list[str] = []
    try:
        payload = json.loads(raw_result)
        urls, b64s, _ = _extract_image_fields(payload)
        urls = [_normalize_image_url(u, image_asset_host=image_asset_host) for u in urls if u.strip()]
        for idx, b64 in enumerate(b64s, start=1):
            try:
                data = base64.b64decode(b64, validate=False)
                img_path = out_dir / f"{prefix}_{ts}_{idx}.png"
                img_path.write_bytes(data)
                saved_files.append(str(img_path))
            except Exception:
                continue
    except Exception:
        pass

    return urls, saved_files, str(meta_path)


def _backup_and_apply_single(file: str, patch_output: str) -> tuple[bool, str, str]:
    ok_b, backup_id, _ = create_backup([file])
    if not ok_b:
        return False, "", "Failed to create backup."
    ok, msg = apply_unified_diff(file, patch_output)
    if ok:
        return True, backup_id, f"{msg} (backup_id={backup_id})"
    return False, backup_id, f"{msg} (backup_id={backup_id})"


def _backup_and_apply_multi(allowed_files: set[str], patch_output: str) -> tuple[bool, str, list[str]]:
    files = sorted(allowed_files)
    ok_b, backup_id, _ = create_backup(files)
    if not ok_b:
        return False, "", ["Failed to create backup."]
    ok, msgs = apply_unified_diff_multi(patch_output, allowed_files=allowed_files)
    msgs.append(f"backup_id={backup_id}")
    return ok, backup_id, msgs


def _clear_wiki_output(wiki_path: Path) -> list[str]:
    messages: list[str] = []
    wiki_dir = Path(wiki_path)
    if not wiki_dir.exists():
        return [f"Wiki dir not found: {wiki_dir}"]

    # 1) clear files first
    for file_path in sorted([p for p in wiki_dir.rglob("*") if p.is_file()], key=lambda p: len(p.parts), reverse=True):
        try:
            file_path.unlink()
            messages.append(f"Removed wiki file: {file_path}")
        except Exception as e:  # noqa: BLE001
            try:
                file_path.write_text("", encoding="utf-8")
                messages.append(f"Truncated locked wiki file: {file_path}")
            except Exception as e2:  # noqa: BLE001
                messages.append(f"Failed clearing wiki file {file_path}: {e}; {e2}")

    # 2) try remove empty dirs (keep root)
    for dir_path in sorted([p for p in wiki_dir.rglob("*") if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
        try:
            dir_path.rmdir()
            messages.append(f"Removed wiki dir: {dir_path}")
        except Exception:
            # directory not empty or locked; keep it
            continue
    return messages


@app.command()
def sync() -> None:
    """Run RAW -> WIKI sync."""
    ensure_workspace()
    result = run_sync()
    wp = result.get("wiki_pages", 0)
    sk = result.get("skipped", 0)
    dl = result.get("deleted", 0)
    console.print(
        f"[green]Sync completed[/green]: changed={result['files']} skipped={sk} deleted={dl} "
        f"chunks={result['chunks']} wiki_pages={wp}"
    )


@app.command()
def where_db() -> None:
    """Show active sqlite path."""
    ensure_workspace()
    console.print(str(resolve_db_path()))


@app.command(name="kb-save")
def kb_save(name: str = typer.Option("", help="Optional backup name suffix")) -> None:
    """Backup knowledge base (raw/wiki/processed)."""
    ensure_workspace()
    cfg = load_config()
    bid, msgs = save_kb_backup(cfg, name=name or None)
    console.print(f"[green]KB backup created:[/green] {bid}")
    for m in msgs:
        console.print(f"[yellow]{m}[/yellow]")


@app.command(name="kb-backups")
def kb_backups(limit: int = typer.Option(20, help="Max backup items")) -> None:
    """List knowledge base backups."""
    ensure_workspace()
    items = list_kb_backups(limit=limit)
    if not items:
        console.print("No KB backups found.")
        return
    for it in items:
        console.print(f"- {it['id']} | {it['created_at']}")


@app.command(name="kb-restore")
def kb_restore(backup_id: str) -> None:
    """Restore knowledge base from backup id."""
    ensure_workspace()
    cfg = load_config()
    ok, msgs = restore_kb_backup(cfg, backup_id)
    for m in msgs:
        console.print(f"[green]{m}[/green]" if m.startswith("Restored") else f"[yellow]{m}[/yellow]")
    if ok:
        console.print("[cyan]KB restore completed.[/cyan]")
    else:
        console.print("[yellow]KB restore completed with warnings/errors.[/yellow]")


@app.command()
def structure() -> None:
    """Show wiki file structure summary."""
    ensure_workspace()
    items = wiki_list_structure()
    if not items:
        console.print("No indexed wiki chunks. Run sync first.")
        return
    for item in items:
        console.print(f"- {item['parent_file']} ({item['chunk_count']} chunks)")


@app.command(name="eval-retrieval")
def eval_retrieval(
    cases: str = typer.Option("data/eval/retrieval_cases.jsonl", help="Path to JSONL eval cases"),
    topk: int = typer.Option(8, help="Top-k retrieval depth"),
    out: str = typer.Option("", help="Optional output report path (.json)"),
) -> None:
    """Run retrieval baseline evaluation against local wiki index."""
    ensure_workspace()
    cfg = load_config()
    path = Path(cases)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    try:
        eval_cases = load_eval_cases(path)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed loading cases:[/red] {e}")
        return

    summary, details = evaluate_retrieval(
        cases=eval_cases,
        topk=topk,
        synonyms_path=cfg.wiki_strategy.synonyms_path,
    )
    console.print(
        f"[green]Retrieval eval[/green]: total={summary['total']} hit={summary['hit']} "
        f"miss={summary['miss']} recall@{summary['topk']}={summary['recall_at_k']} "
        f"top1={summary['top1_accuracy']} mrr={summary['mrr']}"
    )
    for d in details:
        status = "[green]HIT[/green]" if d.hit else "[red]MISS[/red]"
        extra = f" field={d.matched_field}" if d.matched_field else ""
        top = f" top='{d.top_hit}'" if d.top_hit else ""
        rk = f" rank={d.rank}" if d.rank else ""
        console.print(f"- {status} query={d.query!r}{extra}{rk}{top}")
    if out.strip():
        out_path = Path(out)
        if not out_path.is_absolute():
            out_path = (Path.cwd() / out_path).resolve()
        written = save_eval_report(summary, details, out_path)
        console.print(f"[cyan]Report saved:[/cyan] {written}")


@app.command()
def regress(
    cases: str = typer.Option("data/eval/retrieval_cases.jsonl", help="Path to JSONL eval cases"),
    topk: int = typer.Option(8, help="Top-k retrieval depth"),
    out: str = typer.Option("data/eval/reports/latest.json", help="Output report path"),
) -> None:
    """One-click regression: sync then run retrieval eval."""
    ensure_workspace()
    sync_result = run_sync()
    wp = sync_result.get("wiki_pages", 0)
    sk = sync_result.get("skipped", 0)
    dl = sync_result.get("deleted", 0)
    console.print(
        f"[green]Sync completed[/green]: changed={sync_result['files']} skipped={sk} deleted={dl} "
        f"chunks={sync_result['chunks']} wiki_pages={wp}"
    )

    cfg = load_config()
    cases_path = Path(cases)
    if not cases_path.is_absolute():
        cases_path = (Path.cwd() / cases_path).resolve()
    eval_cases = load_eval_cases(cases_path)
    summary, details = evaluate_retrieval(
        cases=eval_cases,
        topk=topk,
        synonyms_path=cfg.wiki_strategy.synonyms_path,
    )
    console.print(
        f"[green]Retrieval eval[/green]: total={summary['total']} hit={summary['hit']} "
        f"miss={summary['miss']} recall@{summary['topk']}={summary['recall_at_k']} "
        f"top1={summary['top1_accuracy']} mrr={summary['mrr']}"
    )
    out_path = Path(out)
    if not out_path.is_absolute():
        out_path = (Path.cwd() / out_path).resolve()
    written = save_eval_report(summary, details, out_path)
    console.print(f"[cyan]Regression report:[/cyan] {written}")


@app.command(name="compare-eval")
def compare_eval(
    base: str = typer.Option("data/eval/reports/baseline.json", help="Baseline report path"),
    current: str = typer.Option("data/eval/reports/latest.json", help="Current report path"),
) -> None:
    """Compare two retrieval eval reports and show metric deltas and query-level changes."""
    ensure_workspace()
    bp = Path(base)
    cp = Path(current)
    if not bp.is_absolute():
        bp = (Path.cwd() / bp).resolve()
    if not cp.is_absolute():
        cp = (Path.cwd() / cp).resolve()

    try:
        b = load_eval_report(bp)
        c = load_eval_report(cp)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Failed loading reports:[/red] {e}")
        return

    comp = compare_eval_reports(b, c)
    d = comp["delta"]
    console.print(
        f"[green]Eval compare[/green]: "
        f"Δrecall={d.get('recall_at_k')} Δtop1={d.get('top1_accuracy')} Δmrr={d.get('mrr')} "
        f"Δhit={d.get('hit')} Δmiss={d.get('miss')}"
    )
    console.print(f"- fixed: {len(comp['fixed_queries'])}")
    for q in comp["fixed_queries"][:20]:
        console.print(f"  [green]+[/green] {q}")
    console.print(f"- regressed: {len(comp['regressed_queries'])}")
    for q in comp["regressed_queries"][:20]:
        console.print(f"  [red]-[/red] {q}")
    console.print(f"- still miss: {len(comp['still_miss_queries'])}")


@app.command(name="set-baseline")
def set_baseline(
    source: str = typer.Option("data/eval/reports/latest.json", help="Source report path"),
    target: str = typer.Option("data/eval/reports/baseline.json", help="Baseline report path"),
) -> None:
    """Copy a report to baseline."""
    ensure_workspace()
    sp = Path(source)
    tp = Path(target)
    if not sp.is_absolute():
        sp = (Path.cwd() / sp).resolve()
    if not tp.is_absolute():
        tp = (Path.cwd() / tp).resolve()
    if not sp.exists():
        console.print(f"[red]Source report not found:[/red] {sp}")
        return
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(sp.read_text(encoding="utf-8-sig"), encoding="utf-8")
    console.print(f"[green]Baseline updated[/green]: {tp}")



@app.command()
def vaultpath(path: str) -> None:
    """Set unified vault path; raw/wiki/processed paths will be derived automatically."""
    ensure_workspace()
    ok, msg = _set_vault_path(path)
    if ok:
        cfg = load_config()
        ensure_workspace(cfg)
        raw_dir = cfg.wiki_strategy.raw_path
        console.print(f"[green]{msg}[/green]")
        console.print(f"[cyan]目录已创建：{cfg.wiki_strategy.vault_path}[/cyan]")
        console.print(f"[cyan]请将知识原文件放入 RAW 子目录：{raw_dir}[/cyan]")
        console.print("[cyan]然后执行同步命令：/sync 或 `wikicoderctl sync`[/cyan]")
    else:
        console.print(f"[red]{msg}[/red]")


@app.command()
def kbclear(
    yes: bool = typer.Option(False, "--yes", help="Confirm clear index"),
    clear_all: bool = typer.Option(False, "--all", help="Also clear generated wiki pages"),
) -> None:
    """Clear wiki index store (chunks + sqlite); optionally clear wiki pages too."""
    ensure_workspace()
    if not yes:
        console.print("[yellow]危险操作：请使用 --yes 确认清空索引。[/yellow]")
        return
    cfg = load_config()
    msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
    if clear_all:
        msgs.extend(_clear_wiki_output(cfg.wiki_strategy.wiki_path))
    for m in msgs:
        console.print(f"[green]{m}[/green]" if m.startswith(("Cleared", "Removed", "Truncated")) else f"[yellow]{m}[/yellow]")
    if clear_all:
        console.print("[cyan]Index and wiki pages cleared (raw kept). Run /sync to rebuild.[/cyan]")
    else:
        console.print("[cyan]已清空索引。可执行 /sync 重新构建。[/cyan]")


@app.command()
def ask(
    query: str,
    trace: bool = typer.Option(False, help="Show tool trace"),
    stream: bool = typer.Option(False, help="Stream output rendering"),
) -> None:
    """Ask in forced wiki mode."""
    ensure_workspace()
    agent = build_agent()
    resp = agent.run(query, force_wiki=True)
    if trace:
        _print_trace(resp.thought, resp.actions)
    _stream_markdown(resp.output, enabled=stream)


@app.command(name="image-understand")
def image_understand(
    image_url: str,
    query: str = typer.Option("请描述这张图并提取关键信息", help="Question for the image"),
) -> None:
    """Use Jiutian image understanding model."""
    ensure_workspace()
    cfg = load_config()
    _ensure_auto_image_models(cfg)
    llm = build_llm(cfg)
    try:
        result = llm.image_understand(prompt=query, image_url=image_url)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/red]")
        return
    # 优先输出文本；若是 JSON 字符串则提取 text 字段
    text_out = result
    try:
        payload = json.loads(result)
        _, _, texts = _extract_image_fields(payload)
        if texts:
            text_out = "\n\n".join(texts[:5])
    except Exception:
        pass
    _stream_markdown(text_out, enabled=False)


@app.command(name="image-generate")
def image_generate(
    prompt: str,
    size: str = typer.Option("1024x1024", help="Image size"),
    save_dir: str = typer.Option("data/generated_images", help="Directory to save result files"),
    prefix: str = typer.Option("imggen", help="Output file prefix"),
) -> None:
    """Use Jiutian image generation model."""
    ensure_workspace()
    cfg = load_config()
    _ensure_auto_image_models(cfg)
    llm = build_llm(cfg)
    try:
        result = llm.image_generate(prompt=prompt, size=size)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/red]")
        return
    urls, saved_files, meta_file = _save_image_result(
        result,
        save_dir=save_dir,
        prefix=prefix,
        image_asset_host=cfg.llm.image_asset_host,
    )

    if saved_files:
        console.print("[green]Saved images:[/green]")
        for f in saved_files:
            console.print(f"- {f}")
    if urls:
        console.print("[cyan]Image URLs:[/cyan]")
        for u in urls:
            console.print(f"- {u}")
    console.print(f"[dim]Raw response saved: {meta_file}[/dim]")
    if not saved_files and not urls:
        _stream_markdown(result, enabled=False)


@app.command(name="xlsx2md")
def xlsx2md(
    path: str = typer.Argument(..., help="xlsx 文件路径，或包含 xlsx 的目录路径"),
    recursive: bool = typer.Option(False, "--recursive", help="目录模式下递归处理子目录"),
) -> None:
    """Convert xlsx file(s) to markdown in the same directory."""
    ensure_workspace()
    outs, errs = convert_xlsx_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")
    if outs and not errs:
        console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")


@app.command(name="pdf2md")
def pdf2md(
    path: str = typer.Argument(..., help="pdf 文件路径，或包含 pdf 的目录路径"),
    recursive: bool = typer.Option(False, "--recursive", help="目录模式下递归处理子目录"),
) -> None:
    """Convert pdf file(s) to markdown in the same directory."""
    ensure_workspace()
    outs, errs = convert_pdf_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")
    if outs and not errs:
        console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")


@app.command(name="docx2md")
def docx2md(
    path: str = typer.Argument(..., help="docx 文件路径，或包含 docx 的目录路径"),
    recursive: bool = typer.Option(False, "--recursive", help="目录模式下递归处理子目录"),
) -> None:
    """Convert docx file(s) to markdown in the same directory."""
    ensure_workspace()
    outs, errs = convert_docx_path(path, recursive=recursive)
    for o in outs:
        console.print(f"[green]已生成：{o}[/green]")
    for e in errs:
        console.print(f"[yellow]{e}[/yellow]")
    if outs and not errs:
        console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")


@app.command()
def review(
    file: str,
    query: str,
    trace: bool = typer.Option(False, help="Show tool trace"),
    stream: bool = typer.Option(False, help="Stream output rendering"),
) -> None:
    """Review a local code file against wiki policy and answer the query."""
    ensure_workspace()
    agent = build_agent()
    code = read_file(file)
    if not code:
        console.print(f"[red]File not found or empty:[/red] {file}")
        return
    code_ctx = f"file: {file}\n```\\n{code}\\n```"
    resp = agent.run(query, force_wiki=True, code_context=code_ctx)
    if trace:
        _print_trace(resp.thought, resp.actions)
    _stream_markdown(resp.output, enabled=stream)


@app.command()
def patch(
    file: str,
    query: str,
    trace: bool = typer.Option(False, help="Show tool trace"),
    stream: bool = typer.Option(False, help="Stream output rendering"),
    apply: bool = typer.Option(False, help="Apply generated patch to file"),
    yes: bool = typer.Option(False, "--yes", help="Confirm applying patch"),
) -> None:
    """Generate a unified diff patch suggestion for a local file."""
    ensure_workspace()
    agent = build_agent()
    code = read_file(file)
    if not code:
        console.print(f"[red]File not found or empty:[/red] {file}")
        return
    code_ctx = f"file: {file}\n```\\n{code}\\n```"
    resp = agent.run(
        query,
        force_wiki=True,
        code_context=code_ctx,
        response_mode="patch",
        target_file=file,
    )
    if trace:
        _print_trace(resp.thought, resp.actions)
    _stream_markdown(resp.output, enabled=stream)
    _print_patch_preview(resp.output)
    if apply:
        if not yes:
            console.print("[yellow]Refused to apply without --yes.[/yellow]")
            return
        ok, _, msg = _backup_and_apply_single(file, resp.output)
        console.print((f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]"))


@app.command(name="patch-multi")
def patch_multi(
    files: str,
    query: str,
    trace: bool = typer.Option(False, help="Show tool trace"),
    stream: bool = typer.Option(False, help="Stream output rendering"),
    apply: bool = typer.Option(False, help="Apply generated patch to files"),
    yes: bool = typer.Option(False, "--yes", help="Confirm applying patch"),
) -> None:
    """Generate multi-file patch suggestion. files is comma-separated paths."""
    ensure_workspace()
    agent = build_agent()
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    if not file_list:
        console.print("[red]No files provided.[/red]")
        return

    blocks: list[str] = []
    for f in file_list:
        code = read_file(f)
        if not code:
            console.print(f"[red]File not found or empty:[/red] {f}")
            return
        blocks.append(f"file: {f}\n```\\n{code}\\n```")

    code_ctx = "\n\n".join(blocks)
    target = ", ".join(file_list)
    resp = agent.run(
        query,
        force_wiki=True,
        code_context=code_ctx,
        response_mode="patch",
        target_file=target,
    )
    if trace:
        _print_trace(resp.thought, resp.actions)
    _stream_markdown(resp.output, enabled=stream)
    _print_patch_preview(resp.output)

    if apply:
        if not yes:
            console.print("[yellow]Refused to apply without --yes.[/yellow]")
            return
        allowed = set(file_list)
        ok, _, msgs = _backup_and_apply_multi(allowed, resp.output)
        for m in msgs:
            console.print(f"[green]{m}[/green]" if m.startswith("Applied") else f"[yellow]{m}[/yellow]")
        if not ok:
            console.print("[yellow]Patch applied partially or with skips/errors.[/yellow]")


@app.command()
def backups(limit: int = typer.Option(20, help="Max backups to list")) -> None:
    """List available backup snapshots."""
    ensure_workspace()
    items = list_backups(limit=limit)
    if not items:
        console.print("No backups found.")
        return
    for it in items:
        console.print(f"- {it['id']} | files={it['file_count']} | {it['created_at']}")


@app.command()
def undo(backup_id: str) -> None:
    """Restore files from a backup snapshot id."""
    ensure_workspace()
    ok, msgs = restore_backup(backup_id)
    for m in msgs:
        console.print(f"[green]{m}[/green]" if m.startswith(("Restored", "Removed", "No-op")) else f"[yellow]{m}[/yellow]")
    if not ok:
        console.print("[yellow]Undo completed with errors.[/yellow]")


@app.command()
def chat(
    trace: bool = typer.Option(False, help="Show tool trace each turn"),
    stream: bool = typer.Option(False, help="Stream output rendering"),
) -> None:
    """Start Claude-like REPL."""
    ensure_workspace()
    config = load_config()
    _print_startup_banner()

    if config.sync.auto_on_startup:
        result = run_sync()
        wp = result.get("wiki_pages", 0)
        sk = result.get("skipped", 0)
        dl = result.get("deleted", 0)
        console.print(
            f"[cyan]Auto sync[/cyan]: changed={result['files']} skipped={sk} deleted={dl} "
            f"chunks={result['chunks']} wiki_pages={wp}"
        )

    agent = build_agent(config)
    session = PromptSession(
        "wikicoder> ",
        completer=SlashCommandCompleter(),
        complete_while_typing=True,
        key_bindings=build_key_bindings(),
    )

    show_trace = trace
    show_stream = stream
    session_mode = "auto"
    last_patch_file = ""
    last_patch_output = ""
    last_patch_allowed: set[str] | None = None
    last_backup_id = ""
    session_history: list[tuple[str, str]] = []
    local_op_consent: dict[str, str] = {"mode": "ask"}
    memory_draft = ""
    memory_title = ""
    _print_runtime_settings(config, session_mode=session_mode)
    console.print(f"[dim]当前会话模式: mode={session_mode}[/dim]")
    if SESSION_STATE_PATH.exists():
        console.print("[dim]检测到上次会话记录，可输入 /resume 继续上下文。[/dim]")

    while True:
        try:
            text = session.prompt()
        except (KeyboardInterrupt, EOFError):
            console.print("\nBye.")
            break

        cmd = text.strip()
        if not cmd:
            continue
        try:  # 最外层异常保护：任何未预期异常不会终止 REPL
            # tolerate commands without leading slash
            if cmd in {
                "sync",
                "help",
                "reset",
                "exit",
                "quit",
                "kbclear",
                "kbclear yes",
                "kbclear all yes",
                "kbbackups",
                "kbsave",
                "resume",
                "memdraft",
                "memsave",
                "model",
                "xlsx2md",
                "pdf2md",
                "docx2md",
            }:
                cmd = f"/{cmd}"

            if cmd in {"/exit", "/quit"}:
                console.print("Bye.")
                break

            if cmd == "/help":
                console.print(
                    "[bold]WikiCoder 命令帮助[/bold]\n\n"
                    "[cyan]一、知识库与同步[/cyan]\n"
                    "/vaultpath <目录>  设置知识库根目录（自动派生 raw/wiki/wiki_processed）\n"
                    "/sync               执行同步（增量）：RAW -> 索引 -> WIKI 页面\n"
                    "/structure          查看当前索引结构（文件与 chunk 数）\n"
                    "/model [name]       查看/切换文本模型（think/chat）\n"
                    "/kbclear yes        清空索引（chunks + sqlite）\n"
                    "/kbclear all yes    清空索引 + wiki 页面（保留 raw 原文件）\n\n"
                    "/kbsave [name]      备份知识库（raw/wiki/processed）\n"
                    "/kbbackups          查看知识库备份列表\n"
                    "/kbrestore <id>     恢复指定知识库备份\n\n"
                    "[cyan]二、问答与模式[/cyan]\n"
                    "/mode auto|wiki_only|general_only  切换会话模式\n"
                    "  - auto: 先检索 wiki，未命中回退通用模型\n"
                    "  - wiki_only: 仅 wiki，不回退\n"
                    "  - general_only: 直接通用模型\n"
                    "/resume             恢复上次会话上下文（最近30轮）\n"
                    "/ask <问题>         强制 Wiki 模式提问\n"
                    "/memdraft [标题]    将本轮会话整理为 wiki 文档草稿\n"
                    "/memsave [标题]     将草稿保存到 raw/faq 目录\n"
                    "自然语言入库示例：写入知识库 标题：xxx 内容：...\n"
                    "/xlsx2md <路径>     将 xlsx 转为同目录同名 md（支持文件或目录）\n"
                    "/pdf2md <路径>      将 pdf 转为同目录同名 md（支持文件或目录）\n"
                    "/docx2md <路径>     将 word(docx) 转为同目录同名 md（支持文件或目录）\n"
                    "/reset              清空当前会话记忆\n\n"
                    "[cyan]三、评测与回归[/cyan]\n"
                    "/eval <cases> [topk] [out]         运行检索评测（recall/top1/mrr）\n"
                    "/regress <cases> [topk] [out]      一键同步 + 评测\n"
                    "/compare <base> <latest>           对比两份评测报告（delta/fixed/regressed）\n"
                    "/baseline <report> [baseline]      将报告设为基线\n\n"
                    "[cyan]四、代码审阅与补丁[/cyan]\n"
                    "/review <文件> :: <问题>            按知识库规则审阅文件\n"
                    "/patch <文件> :: <需求>             生成单文件补丁\n"
                    "/patchm <f1,f2> :: <需求>           生成多文件补丁\n"
                    "/preview                            预览最近补丁摘要\n"
                    "/apply yes                          应用最近补丁\n"
                    "/backups                            查看备份列表\n"
                    "/undo [backup_id]                   回滚备份\n\n"
                    "[cyan]五、显示与会话[/cyan]\n"
                    "提问处理中会显示耗时秒数，可按 ESC 取消本次提问\n"
                    "普通对话中如为脚本类需求：先结构探测 -> 再生成脚本 -> 执行并持续自动修复\n"
                    "本地写入/执行前会询问授权：y(本次) / a(本会话全部同意) / n(拒绝)\n"
                    "高级命令请执行：/help advanced\n"
                    "/exit               退出 CLI"
                )
                continue

            if cmd == "/help advanced":
                console.print(
                    "[bold]WikiCoder 高级命令[/bold]\n\n"
                    "[cyan]评测与回归[/cyan]\n"
                    "/eval <cases> [topk] [out]\n"
                    "/regress <cases> [topk] [out]\n"
                    "/compare <base> <latest>\n"
                    "/baseline <report> [baseline]\n\n"
                    "[cyan]代码补丁工作流[/cyan]\n"
                    "/review <文件> :: <问题>\n"
                    "/patch <文件> :: <需求>\n"
                    "/patchm <f1,f2> :: <需求>\n"
                    "/preview\n"
                    "/apply yes\n"
                    "/backups\n"
                    "/undo <backup_id>\n\n"
                    "[cyan]显示控制[/cyan]\n"
                    "/trace on|off\n"
                    "/stream on|off"
                )
                continue

            if cmd == "/sync":
                result = run_sync()
                wp = result.get("wiki_pages", 0)
                sk = result.get("skipped", 0)
                dl = result.get("deleted", 0)
                console.print(
                    f"[green]Sync completed[/green]: changed={result['files']} skipped={sk} deleted={dl} "
                    f"chunks={result['chunks']} wiki_pages={wp}"
                )
                continue

            if cmd == "/xlsx2md" or cmd.startswith("/xlsx2md "):
                if cmd == "/xlsx2md":
                    console.print("[yellow]用法：/xlsx2md <文件或目录路径>[/yellow]")
                    continue
                arg = cmd.split(" ", 1)[1].strip()
                recursive = False
                if arg.endswith(" -r") or arg.endswith(" --recursive"):
                    recursive = True
                    arg = arg.rsplit(" ", 1)[0].strip()
                outs, errs = convert_xlsx_path(arg, recursive=recursive)
                for o in outs:
                    console.print(f"[green]已生成：{o}[/green]")
                for e in errs:
                    console.print(f"[yellow]{e}[/yellow]")
                if outs and not errs:
                    console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")
                continue

            if cmd == "/pdf2md" or cmd.startswith("/pdf2md "):
                if cmd == "/pdf2md":
                    console.print("[yellow]用法：/pdf2md <文件或目录路径>[/yellow]")
                    continue
                arg = cmd.split(" ", 1)[1].strip()
                recursive = False
                if arg.endswith(" -r") or arg.endswith(" --recursive"):
                    recursive = True
                    arg = arg.rsplit(" ", 1)[0].strip()
                outs, errs = convert_pdf_path(arg, recursive=recursive)
                for o in outs:
                    console.print(f"[green]已生成：{o}[/green]")
                for e in errs:
                    console.print(f"[yellow]{e}[/yellow]")
                if outs and not errs:
                    console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")
                continue

            if cmd == "/docx2md" or cmd.startswith("/docx2md "):
                if cmd == "/docx2md":
                    console.print("[yellow]用法：/docx2md <文件或目录路径>[/yellow]")
                    continue
                arg = cmd.split(" ", 1)[1].strip()
                recursive = False
                if arg.endswith(" -r") or arg.endswith(" --recursive"):
                    recursive = True
                    arg = arg.rsplit(" ", 1)[0].strip()
                outs, errs = convert_docx_path(arg, recursive=recursive)
                for o in outs:
                    console.print(f"[green]已生成：{o}[/green]")
                for e in errs:
                    console.print(f"[yellow]{e}[/yellow]")
                if outs and not errs:
                    console.print(f"[cyan]完成，共转换 {len(outs)} 个文件。[/cyan]")
                continue

            if cmd == "/md2canvas" or cmd.startswith("/md2canvas ") or cmd.startswith("/md2canvas_ai"):
                from src.skills.canvas_tools import handle_canvas_command
                handle_canvas_command(cmd)
                continue

            if cmd in {"/kbclear", "/kbclear yes", "/kbclear all yes"}:
                if cmd == "/kbclear":
                    console.print("[yellow]危险操作，请使用 /kbclear yes 或 /kbclear all yes 确认。[/yellow]")
                    continue
                clear_all = cmd == "/kbclear all yes"
                cfg = load_config()
                msgs = clear_index_store(processed_path=cfg.wiki_strategy.processed_path)
                if clear_all:
                    msgs.extend(_clear_wiki_output(cfg.wiki_strategy.wiki_path))
                for m in msgs:
                    console.print(
                        f"[green]{m}[/green]"
                        if m.startswith(("Cleared", "Removed", "Truncated"))
                        else f"[yellow]{m}[/yellow]"
                    )
                if clear_all:
                    console.print("[cyan]已清空索引和 wiki 生成页（raw 未删除）。可执行 /sync 重新构建。[/cyan]")
                else:
                    console.print("[cyan]已清空索引。可执行 /sync 重新构建。[/cyan]")
                continue

            if cmd == "/kbbackups":
                items = list_kb_backups(limit=30)
                if not items:
                    console.print("No KB backups found.")
                else:
                    for it in items:
                        console.print(f"- {it['id']} | {it['created_at']}")
                continue

            if cmd == "/kbsave" or cmd.startswith("/kbsave "):
                name = cmd.split(" ", 1)[1].strip() if cmd.startswith("/kbsave ") else ""
                cfg = load_config()
                bid, msgs = save_kb_backup(cfg, name=name or None)
                console.print(f"[green]KB backup created:[/green] {bid}")
                for m in msgs:
                    console.print(f"[yellow]{m}[/yellow]")
                continue

            if cmd.startswith("/kbrestore "):
                backup_id = cmd.split(" ", 1)[1].strip()
                if not backup_id:
                    console.print("[yellow]Usage: /kbrestore <backup_id>[/yellow]")
                    continue
                cfg = load_config()
                ok, msgs = restore_kb_backup(cfg, backup_id)
                for m in msgs:
                    console.print(f"[green]{m}[/green]" if m.startswith("Restored") else f"[yellow]{m}[/yellow]")
                if ok:
                    console.print("[cyan]KB restore completed.[/cyan]")
                else:
                    console.print("[yellow]KB restore completed with warnings/errors.[/yellow]")
                continue


            if cmd == "/structure":
                items = wiki_list_structure()
                if not items:
                    console.print("No indexed wiki chunks.")
                else:
                    for item in items:
                        console.print(f"- {item['parent_file']} ({item['chunk_count']} chunks)")
                continue

            if cmd == "/eval" or cmd.startswith("/eval "):
                parts = cmd.split()
                cases_path = "data/eval/retrieval_cases.jsonl"
                topk_n = 8
                out_path = ""
                if len(parts) >= 2:
                    cases_path = parts[1]
                if len(parts) >= 3:
                    try:
                        topk_n = max(1, int(parts[2]))
                    except Exception:
                        console.print("[yellow]Usage: /eval <cases.jsonl> [topk] [out.json][/yellow]")
                        continue
                if len(parts) >= 4:
                    out_path = parts[3]
                pth = Path(cases_path)
                if not pth.is_absolute():
                    pth = (Path.cwd() / pth).resolve()
                try:
                    eval_cases = load_eval_cases(pth)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]Failed loading cases:[/red] {e}")
                    continue
                summary, details = evaluate_retrieval(
                    cases=eval_cases,
                    topk=topk_n,
                    synonyms_path=config.wiki_strategy.synonyms_path,
                )
                console.print(
                    f"[green]Retrieval eval[/green]: total={summary['total']} hit={summary['hit']} "
                    f"miss={summary['miss']} recall@{summary['topk']}={summary['recall_at_k']} "
                    f"top1={summary['top1_accuracy']} mrr={summary['mrr']}"
                )
                for d in details:
                    status = "[green]HIT[/green]" if d.hit else "[red]MISS[/red]"
                    extra = f" field={d.matched_field}" if d.matched_field else ""
                    rk = f" rank={d.rank}" if d.rank else ""
                    top = f" top='{d.top_hit}'" if d.top_hit else ""
                    console.print(f"- {status} query={d.query!r}{extra}{rk}{top}")
                if out_path.strip():
                    op = Path(out_path)
                    if not op.is_absolute():
                        op = (Path.cwd() / op).resolve()
                    written = save_eval_report(summary, details, op)
                    console.print(f"[cyan]Report saved:[/cyan] {written}")
                continue

            if cmd == "/regress" or cmd.startswith("/regress "):
                parts = cmd.split()
                cases_path = "data/eval/retrieval_cases.jsonl"
                topk_n = 8
                out_path = "data/eval/reports/latest.json"
                if len(parts) >= 2:
                    cases_path = parts[1]
                if len(parts) >= 3:
                    try:
                        topk_n = max(1, int(parts[2]))
                    except Exception:
                        console.print("[yellow]Usage: /regress <cases.jsonl> [topk] [out.json][/yellow]")
                        continue
                if len(parts) >= 4:
                    out_path = parts[3]

                result = run_sync()
                wp = result.get("wiki_pages", 0)
                sk = result.get("skipped", 0)
                dl = result.get("deleted", 0)
                console.print(
                    f"[green]Sync completed[/green]: changed={result['files']} skipped={sk} deleted={dl} "
                    f"chunks={result['chunks']} wiki_pages={wp}"
                )

                pth = Path(cases_path)
                if not pth.is_absolute():
                    pth = (Path.cwd() / pth).resolve()
                try:
                    eval_cases = load_eval_cases(pth)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]Failed loading cases:[/red] {e}")
                    continue

                summary, details = evaluate_retrieval(
                    cases=eval_cases,
                    topk=topk_n,
                    synonyms_path=config.wiki_strategy.synonyms_path,
                )
                console.print(
                    f"[green]Retrieval eval[/green]: total={summary['total']} hit={summary['hit']} "
                    f"miss={summary['miss']} recall@{summary['topk']}={summary['recall_at_k']} "
                    f"top1={summary['top1_accuracy']} mrr={summary['mrr']}"
                )
                op = Path(out_path)
                if not op.is_absolute():
                    op = (Path.cwd() / op).resolve()
                written = save_eval_report(summary, details, op)
                console.print(f"[cyan]Regression report:[/cyan] {written}")
                continue

            if cmd == "/compare" or cmd.startswith("/compare "):
                parts = cmd.split()
                base_path = "data/eval/reports/baseline.json"
                current_path = "data/eval/reports/latest.json"
                if len(parts) >= 2:
                    base_path = parts[1]
                if len(parts) >= 3:
                    current_path = parts[2]
                bp = Path(base_path)
                cp = Path(current_path)
                if not bp.is_absolute():
                    bp = (Path.cwd() / bp).resolve()
                if not cp.is_absolute():
                    cp = (Path.cwd() / cp).resolve()
                try:
                    b = load_eval_report(bp)
                    c = load_eval_report(cp)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]Failed loading reports:[/red] {e}")
                    continue

                comp = compare_eval_reports(b, c)
                d = comp["delta"]
                console.print(
                    f"[green]Eval compare[/green]: "
                    f"Δrecall={d.get('recall_at_k')} Δtop1={d.get('top1_accuracy')} Δmrr={d.get('mrr')} "
                    f"Δhit={d.get('hit')} Δmiss={d.get('miss')}"
                )
                console.print(f"- fixed={len(comp['fixed_queries'])} regressed={len(comp['regressed_queries'])} "
                              f"still_miss={len(comp['still_miss_queries'])}")
                continue

            if cmd == "/baseline" or cmd.startswith("/baseline "):
                parts = cmd.split()
                src = "data/eval/reports/latest.json"
                dst = "data/eval/reports/baseline.json"
                if len(parts) >= 2:
                    src = parts[1]
                if len(parts) >= 3:
                    dst = parts[2]
                sp = Path(src)
                tp = Path(dst)
                if not sp.is_absolute():
                    sp = (Path.cwd() / sp).resolve()
                if not tp.is_absolute():
                    tp = (Path.cwd() / tp).resolve()
                if not sp.exists():
                    console.print(f"[red]Source report not found:[/red] {sp}")
                    continue
                tp.parent.mkdir(parents=True, exist_ok=True)
                tp.write_text(sp.read_text(encoding="utf-8-sig"), encoding="utf-8")
                console.print(f"[green]Baseline updated[/green]: {tp}")
                continue


            if cmd.startswith("/vaultpath "):
                new_path = cmd[len("/vaultpath ") :].strip()
                ok, msg = _set_vault_path(new_path)
                if ok:
                    config = load_config()
                    ensure_workspace(config)
                    console.print(f"[green]{msg}[/green]")
                    console.print(f"[cyan]目录已创建：{config.wiki_strategy.vault_path}[/cyan]")
                    console.print(f"[cyan]请将知识原文件放入 RAW 子目录：{config.wiki_strategy.raw_path}[/cyan]")
                    console.print("[cyan]然后执行同步命令：/sync[/cyan]")
                else:
                    console.print(f"[red]{msg}[/red]")
                continue
            if cmd == "/preview":
                if not last_patch_output:
                    console.print("No patch available. Run /patch or /patchm first.")
                    continue
                _print_patch_preview(last_patch_output)
                continue

            if cmd == "/backups":
                items = list_backups(limit=20)
                if not items:
                    console.print("No backups found.")
                else:
                    for it in items:
                        console.print(f"- {it['id']} | files={it['file_count']} | {it['created_at']}")
                continue

            if cmd == "/undo" or cmd.startswith("/undo "):
                bid = cmd.split(" ", 1)[1].strip() if cmd.startswith("/undo ") else last_backup_id
                if not bid:
                    console.print("No backup id provided and no recent backup in session.")
                    continue
                ok, msgs = restore_backup(bid)
                for m in msgs:
                    console.print(f"[green]{m}[/green]" if m.startswith(("Restored", "Removed", "No-op")) else f"[yellow]{m}[/yellow]")
                if not ok:
                    console.print("[yellow]Undo completed with errors.[/yellow]")
                continue

            if cmd == "/apply" or cmd == "/apply yes":
                if not last_patch_file or not last_patch_output:
                    console.print("No patch to apply. Run /patch first.")
                    continue
                if cmd != "/apply yes":
                    console.print("[yellow]Use /apply yes to confirm applying patch.[/yellow]")
                    _print_patch_preview(last_patch_output)
                    continue
                if last_patch_allowed:
                    ok, bid, msgs = _backup_and_apply_multi(last_patch_allowed, last_patch_output)
                    last_backup_id = bid
                    for m in msgs:
                        console.print(f"[green]{m}[/green]" if m.startswith("Applied") else f"[yellow]{m}[/yellow]")
                    if not ok:
                        console.print("[yellow]Patch applied partially or with skips/errors.[/yellow]")
                else:
                    ok, bid, msg = _backup_and_apply_single(last_patch_file, last_patch_output)
                    last_backup_id = bid
                    console.print((f"[green]{msg}[/green]" if ok else f"[red]{msg}[/red]"))
                continue

            if cmd.startswith("/trace "):
                val = cmd.split(" ", 1)[1].strip().lower()
                if val in {"on", "off"}:
                    show_trace = val == "on"
                    console.print(f"trace={show_trace}")
                else:
                    console.print("Usage: /trace on|off")
                continue

            if cmd.startswith("/stream "):
                val = cmd.split(" ", 1)[1].strip().lower()
                if val in {"on", "off"}:
                    show_stream = val == "on"
                    console.print(f"stream={show_stream}")
                else:
                    console.print("Usage: /stream on|off")
                continue

            if cmd.startswith("/mode "):
                val = cmd.split(" ", 1)[1].strip().lower()
                if val not in {"auto", "wiki_only", "general_only", "build"}:
                    console.print("[yellow]Usage: /mode auto|wiki_only|general_only|build[/yellow]")
                    continue
                session_mode = val
                console.print(f"[cyan]session mode = {session_mode}[/cyan]")
                _save_session_state(session_history, mode=session_mode)
                continue

            if cmd == "/model":
                config = load_config()
                _print_runtime_settings(config, session_mode=session_mode)
                console.print(
                    "[cyan]可切换：/model jiutian-think-v3 | /model jiutian-lan-comv3[/cyan]\n"
                    "[dim]图片理解/图片生成模型会根据问题自动切换。[/dim]"
                )
                continue

            if cmd.startswith("/model "):
                model_name = cmd.split(" ", 1)[1].strip()
                ok, msg = _set_model_config(model_name)
                if not ok:
                    console.print(f"[yellow]{msg}[/yellow]")
                    continue
                console.print(f"[green]{msg}[/green]")
                config = load_config()
                agent = build_agent(config)
                _print_runtime_settings(config, session_mode=session_mode)
                continue

            if cmd == "/resume":
                old_hist, old_mode = _load_session_state()
                if not old_hist:
                    console.print("[yellow]没有可恢复的上次会话记录。[/yellow]")
                    continue
                session_history = old_hist
                session_mode = old_mode
                console.print(f"[green]已恢复上次会话[/green]：{len(session_history)} 轮，mode={session_mode}")
                _replay_session_on_screen(session_history)
                continue

            if cmd == "/reset":
                session_history = []
                console.print("[cyan]已清空会话上下文记忆。[/cyan]")
                _clear_session_state_file()
                continue

            if cmd == "/memdraft" or cmd.startswith("/memdraft "):
                title_hint = cmd.split(" ", 1)[1].strip() if cmd.startswith("/memdraft ") else ""
                if not session_history:
                    console.print("[yellow]当前会话暂无可整理内容。请先进行几轮问答。[/yellow]")
                    continue
                llm = build_llm(config)
                hist_text = "\n\n".join(
                    [f"### 用户问题\n{q}\n\n### 助手回答\n{a}" for q, a in session_history[-12:]]
                )
                system_prompt = (
                    "你是知识工程师。请把给定对话整理为可直接入库的中文 Wiki Markdown 文档。"
                    "要求：结构清晰、可复用、避免口语、不要编造事实。"
                )
                user_prompt = (
                    f"文档标题建议：{title_hint or '自动整理会话'}\n\n"
                    "请严格按以下结构输出 Markdown：\n"
                    "# 标题\n"
                    "## 背景\n"
                    "## 结论\n"
                    "## 详细说明\n"
                    "## 操作步骤\n"
                    "## 注意事项\n"
                    "## 标签\n"
                    "对话内容如下：\n\n"
                    f"{hist_text}"
                )
                try:
                    draft = _run_llm_with_thinking(
                        llm,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        phase="整理会话为Wiki文档中",
                    ).strip()
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]生成草稿失败：{e}[/red]")
                    continue
                if not draft:
                    console.print("[yellow]已取消或草稿为空，请重试。[/yellow]")
                    continue
                memory_draft = draft
                m = re.search(r"^#\s+(.+)$", draft, flags=re.MULTILINE)
                memory_title = (m.group(1).strip() if m else title_hint or "会话整理")
                console.print(f"[green]已生成草稿[/green]：{memory_title}")
                _stream_markdown(draft, enabled=False)
                console.print("[cyan]可执行 /memsave [标题] 保存到 raw/faq，并随后 /sync[/cyan]")
                continue

            if cmd == "/memsave" or cmd.startswith("/memsave "):
                if not memory_draft:
                    console.print("[yellow]当前没有草稿。请先执行 /memdraft[/yellow]")
                    continue
                title = cmd.split(" ", 1)[1].strip() if cmd.startswith("/memsave ") else memory_title
                title = title or memory_title or "会话整理"
                try:
                    out = _save_memory_markdown(config, title, memory_draft)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]保存失败：{e}[/red]")
                    continue
                console.print(f"[green]已保存：{out}[/green]")
                console.print("[cyan]请执行 /sync 将该记忆纳入检索。[/cyan]")
                continue

            remember_turn = False
            plain_chat_turn = False
            if cmd.startswith("/ask "):
                query = cmd[5:].strip()
                console.print(f"[black on bright_cyan] You: {query} [/black on bright_cyan]")
                resp = _run_agent_with_thinking(
                    agent,
                    user_input=query,
                    force_wiki=True,
                    history=session_history,
                    mode="wiki_only",
                )
                remember_turn = True
            elif cmd.startswith("/review "):
                body = cmd[len("/review ") :].strip()
                if "::" not in body:
                    console.print("Usage: /review <file> :: <query>")
                    continue
                file, query = [x.strip() for x in body.split("::", 1)]
                code = read_file(file)
                if not code:
                    console.print(f"[red]File not found or empty:[/red] {file}")
                    continue
                extra_ctx = _build_cross_file_context(query, exclude_files={file})
                code_ctx = f"file: {file}\n```\\n{code}\\n```"
                if extra_ctx:
                    code_ctx += "\n\n[Cross-file context]\n" + extra_ctx
                console.print(f"[black on bright_cyan] You: {query} [/black on bright_cyan]")
                resp = _run_agent_with_thinking(
                    agent,
                    user_input=query,
                    force_wiki=True,
                    code_context=code_ctx,
                    history=session_history,
                    mode="wiki_only",
                )
            elif cmd.startswith("/patch "):
                body = cmd[len("/patch ") :].strip()
                if "::" not in body:
                    console.print("Usage: /patch <file> :: <query>")
                    continue
                file, query = [x.strip() for x in body.split("::", 1)]
                code = read_file(file)
                if not code:
                    console.print(f"[red]File not found or empty:[/red] {file}")
                    continue
                extra_ctx = _build_cross_file_context(query, exclude_files={file})
                code_ctx = f"file: {file}\n```\\n{code}\\n```"
                if extra_ctx:
                    code_ctx += "\n\n[Cross-file context]\n" + extra_ctx
                console.print(f"[black on bright_cyan] You: {query} [/black on bright_cyan]")
                resp = _run_agent_with_thinking(
                    agent,
                    user_input=query,
                    force_wiki=True,
                    code_context=code_ctx,
                    response_mode="patch",
                    target_file=file,
                    history=session_history,
                    mode="wiki_only",
                )
                last_patch_file = file
                last_patch_output = resp.output
                last_patch_allowed = None
            elif cmd.startswith("/patchm "):
                body = cmd[len("/patchm ") :].strip()
                if "::" not in body:
                    console.print("Usage: /patchm <file1,file2> :: <query>")
                    continue
                files_part, query = [x.strip() for x in body.split("::", 1)]
                file_list = [f.strip() for f in files_part.split(",") if f.strip()]
                if not file_list:
                    console.print("Usage: /patchm <file1,file2> :: <query>")
                    continue
                blocks: list[str] = []
                missing = False
                for f in file_list:
                    code = read_file(f)
                    if not code:
                        console.print(f"[red]File not found or empty:[/red] {f}")
                        missing = True
                        break
                    blocks.append(f"file: {f}\n```\\n{code}\\n```")
                if missing:
                    continue
                extra_ctx = _build_cross_file_context(query, exclude_files=set(file_list))
                code_ctx = "\n\n".join(blocks)
                if extra_ctx:
                    code_ctx += "\n\n[Cross-file context]\n" + extra_ctx
                console.print(f"[black on bright_cyan] You: {query} [/black on bright_cyan]")
                resp = _run_agent_with_thinking(
                    agent,
                    user_input=query,
                    force_wiki=True,
                    code_context=code_ctx,
                    response_mode="patch",
                    target_file=", ".join(file_list),
                    history=session_history,
                    mode="wiki_only",
                )
                last_patch_file = file_list[0]
                last_patch_output = resp.output
                last_patch_allowed = set(file_list)
            else:
                console.print(f"[black on bright_cyan] You: {cmd} [/black on bright_cyan]")
                if _looks_like_kb_save_request(cmd):
                    title_hint = _extract_kb_title(cmd)
                    content_text = _extract_kb_content(cmd)
                    if content_text:
                        title, markdown_text = _normalize_kb_markdown(content_text, title_hint=title_hint)
                    else:
                        title, markdown_text = _build_kb_markdown_from_last_turn(session_history, title_hint=title_hint)
                    if not markdown_text:
                        resp = AgentResponse(
                            thought="kb_save:no-history",
                            actions=["kb_save:skipped"],
                            output=(
                                "当前没有可写入知识库的内容。\n"
                                "可先进行一次问答，或直接输入：\n"
                                "写入知识库 标题：xxx 内容：..."
                            ),
                        )
                        remember_turn = False
                    else:
                        try:
                            cfg_now = load_config()
                            out = _save_memory_markdown(cfg_now, title, markdown_text)
                            resp = AgentResponse(
                                thought="kb_save:ok",
                                actions=[f"kb_save(path='{out}')"],
                                output=(
                                    "已实际写入本地知识库文件：\n"
                                    f"{out}\n\n"
                                    "你可继续执行 /sync 将其纳入检索。"
                                ),
                            )
                        except Exception as e:  # noqa: BLE001
                            resp = AgentResponse(
                                thought="kb_save:failed",
                                actions=["kb_save:error"],
                                output=f"写入本地知识库失败：{e}",
                            )
                        remember_turn = True
                    plain_chat_turn = False
                elif _looks_like_image_understand_request(cmd):
                    config = load_config()
                    _ensure_auto_image_models(config)
                    llm = build_llm(config)
                    q, img_url = _extract_image_understand_prompt(cmd)
                    try:
                        result = _run_image_understand_with_thinking(
                            llm,
                            prompt=q,
                            image_url=img_url,
                            phase="图片理解中",
                        )
                        if not result:
                            resp = AgentResponse(
                                thought="cancelled-by-user",
                                actions=["cancelled: ESC pressed"],
                                output="已取消本次图片理解。",
                            )
                            remember_turn = False
                        else:
                            text_out = result
                            try:
                                payload = json.loads(result)
                                _, _, texts = _extract_image_fields(payload)
                                if texts:
                                    text_out = "\n\n".join(texts[:5])
                            except Exception:
                                pass
                            resp = AgentResponse(
                                thought=f"image_understand(provider={config.llm.provider}, model={config.llm.image_understand_model})",
                                actions=[f"image_understand(url='{img_url[:60]}...')"],
                                output=text_out or "图片理解完成，但未返回可读文本。",
                            )
                            remember_turn = True
                    except Exception as e:  # noqa: BLE001
                        resp = AgentResponse(
                            thought="image_understand:failed",
                            actions=["image_understand:error"],
                            output=f"图片理解失败：{e}",
                        )
                        remember_turn = True
                    plain_chat_turn = False
                elif _looks_like_image_generate_request(cmd):
                    config = load_config()
                    _ensure_auto_image_models(config)
                    llm = build_llm(config)
                    img_prompt = _extract_image_generate_prompt(cmd)
                    try:
                        result = _run_image_generate_with_thinking(
                            llm,
                            prompt=img_prompt,
                            size="1024x1024",
                            phase="图片生成中",
                        )
                        if not result:
                            resp = AgentResponse(
                                thought="cancelled-by-user",
                                actions=["cancelled: ESC pressed"],
                                output="已取消本次图片生成。",
                            )
                            remember_turn = False
                        else:
                            urls, saved_files, meta_file = _save_image_result(
                                result,
                                save_dir="data/generated_images",
                                prefix="imggen",
                                image_asset_host=config.llm.image_asset_host,
                            )
                            lines = [f"已完成图片生成（模型：{config.llm.image_generate_model}）。"]
                            if saved_files:
                                lines.append("\n保存文件：")
                                lines.extend([f"- {p}" for p in saved_files])
                            if urls:
                                lines.append("\n图片链接：")
                                lines.extend([f"- {u}" for u in urls])
                            lines.append(f"\n原始响应保存：{meta_file}")
                            resp = AgentResponse(
                                thought=f"image_generate(provider={config.llm.provider}, model={config.llm.image_generate_model})",
                                actions=[f"image_generate(prompt='{img_prompt[:60]}...')"],
                                output="\n".join(lines),
                            )
                            remember_turn = True
                    except Exception as e:  # noqa: BLE001
                        resp = AgentResponse(
                            thought="image_generate:failed",
                            actions=["image_generate:error"],
                            output=f"图片生成失败：{e}",
                        )
                        remember_turn = True
                    plain_chat_turn = False
                elif session_mode == "build":
                    console.print("[bold cyan]>>> 进入 Build 模式 (交互式命令流模式)[/bold cyan]")
                    agent_build = BuildAgent(config)
                    
                    # 使用闭包变量记录“全部同意”状态
                    state = {"auto_all": False}

                    def _cli_on_step(step: BuildStep) -> bool:
                        console.print(f"\n[bold yellow]思考:[/bold yellow] {step.thought}")
                        console.print(f"[bold magenta]拟执行:[/bold magenta] {step.action_type}({step.action_input})")
                        if step.action_type == "finish":
                            return True
                        
                        if state["auto_all"]:
                            # 自动执行时也打印执行中提示
                            console.print(f"[dim]正在自动执行 {step.action_type}...[/dim]")
                        else:
                            ans = console.input("[bold green]授权执行? (y/a/n): [/bold green]").strip().lower()
                            if ans == 'a':
                                state["auto_all"] = True
                            elif ans == 'n': return False
                        
                        # 真正的执行将在 run 方法内部发生，我们通过回调后的步骤历史获取结果
                        return True

                    try:
                        # 劫持 BuildAgent._execute 以便在 CLI 中实时打印结果
                        original_execute = agent_build._execute
                        def _cli_execute_wrapper(action_type, action_input):
                            res = original_execute(action_type, action_input)
                            console.print(f"[bold green]执行结果:[/bold green]\n{res}")
                            return res
                        agent_build._execute = _cli_execute_wrapper

                        final_output = agent_build.run(cmd, history=session_history, on_step=_cli_on_step)
                        resp = AgentResponse(
                            thought="build-mode:complete",
                            actions=["build:done"],
                            output=final_output
                        )
                    except Exception as e:
                        console.print(f"[red]执行异常：{e}[/red]")
                        resp = AgentResponse(thought="build:error", actions=[], output=str(e))
                    
                    remember_turn = True
                    plain_chat_turn = False
                else:
                    auto_ctx = _extract_existing_py_context(cmd)
                    if _looks_like_script_request(cmd):
                        xctx = _build_cross_file_context(cmd)
                        if xctx:
                            auto_ctx = (auto_ctx + "\n\n" if auto_ctx else "") + "[Cross-file context]\n" + xctx
                    resp = _run_agent_with_thinking(
                        agent,
                        user_input=cmd,
                        force_wiki=False,
                        code_context=auto_ctx,
                        history=session_history,
                        mode=session_mode,
                    )
                    remember_turn = True
                    plain_chat_turn = True

            if plain_chat_turn and resp.thought != "cancelled-by-user":
                if _looks_like_script_request(cmd) and session_mode != "build":
                    console.print("\n[bold yellow]检测到编码/自动化需求。[/bold yellow]")
                    console.print("[yellow]当前处于常规对话模式。如需执行自动化任务，请先输入 [bold]/mode build[/bold] 切换到构建模式。[/yellow]\n")
                else:
                    try:
                        resp = _auto_script_pipeline(
                            agent=agent,
                            user_query=cmd,
                            resp=resp,
                            history=session_history,
                            consent_state=local_op_consent,
                        )
                    except Exception:  # noqa: BLE001
                        pass  # pipeline 内部已有异常保护，此处兜底防崩溃

            is_patch_cmd = cmd.startswith("/patch ") or cmd.startswith("/patchm ")
            if is_patch_cmd and resp.thought != "cancelled-by-user":
                _print_patch_preview(resp.output)
                target_desc = ", ".join(sorted(last_patch_allowed)) if last_patch_allowed else last_patch_file
                if target_desc and _confirm_local_operation(local_op_consent, f"立即应用补丁到：{target_desc}"):
                    if last_patch_allowed:
                        ok_apply, bid, msgs = _backup_and_apply_multi(last_patch_allowed, resp.output)
                        last_backup_id = bid
                        apply_msg = "\n".join(msgs)
                    else:
                        ok_apply, bid, apply_msg = _backup_and_apply_single(last_patch_file, resp.output)
                        last_backup_id = bid
                    status_line = "[补丁应用] 成功" if ok_apply else "[补丁应用] 失败"
                    resp.output = f"{resp.output}\n\n---\n{status_line}\n{apply_msg}"
                    if ok_apply:
                        last_patch_file = ""
                        last_patch_output = ""
                        last_patch_allowed = None

            if show_trace:
                _print_trace(resp.thought, resp.actions)

            if resp.thought == "cancelled-by-user":
                remember_turn = False

            if not getattr(resp, "_already_streamed", False):
                _stream_markdown(resp.output, enabled=show_stream)
            if remember_turn:
                session_history.append((cmd, resp.output))
                if len(session_history) > 20:
                    session_history = session_history[-20:]
                _save_session_state(session_history, mode=session_mode)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中断当前操作，可继续输入。[/yellow]")
            continue
        except SystemExit:
            break
        except BaseException as e:  # noqa: BLE001
            console.print(f"\n[red]未预期异常（会话不中断）：{type(e).__name__}: {e}[/red]")
            console.print("[dim]可继续输入命令。如问题持续，请检查 config.yaml 或重启。[/dim]")
            continue



@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8000, help="监听端口"),
):
    """启动 Wikicodian 后端 Web 服务"""
    from src.core.web_api import start_server
    console.print(f"[bold green]Wikicodian 服务启动中...[/bold green] 地址: http://{host}:{port}")
    start_server(host=host, port=port)


if __name__ == "__main__":
    app()


def run_cli() -> None:
    """Console entry: start REPL directly with one command."""
    chat(trace=False, stream=False)
