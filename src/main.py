from __future__ import annotations

import base64
import json
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

from src.core.agent import WikiFirstAgent
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
)
from src.skills.wiki_tools import wiki_list_structure
from src.utils.config import AppConfig, DEFAULT_CONFIG_PATH, ensure_workspace, load_config
from src.utils.db_manager import clear_index_store, resolve_db_path


app = typer.Typer(help="WikiCoder CLI")
console = Console()


class SlashCommandCompleter(Completer):
    def __init__(self) -> None:
        self.commands = [
            ("/help", "??????"),
            ("/sync", "??????RAW -> WIKI?"),
            ("/kbclear yes", "?????? yes ???"),
            ("/kbclear all yes", "???? + wiki ????? raw?"),
            ("/vaultpath ", "?????????/vaultpath <??>"),
            ("/ask ", "?? Wiki ????"),
            ("/review ", "?????/review <??> :: <??>"),
            ("/patch ", "?????/patch <??> :: <??>"),
            ("/patchm ", "??????/patchm <f1,f2> :: <??>"),
            ("/preview", "??????"),
            ("/apply yes", "??????"),
            ("/backups", "????"),
            ("/undo ", "??? ID ??"),
            ("/eval ", "?????/eval <cases.jsonl> [topk] [out.json]"),
            ("/regress ", "??????????"),
            ("/compare ", "?????/compare <baseline.json> <latest.json>"),
            ("/baseline ", "???????/baseline <report.json>"),
            ("/structure", "??????"),
            ("/trace on", "?? trace"),
            ("/trace off", "?? trace"),
            ("/stream on", "??????"),
            ("/stream off", "??????"),
            ("/mode ", "?????/mode auto|wiki_only|general_only"),
            ("/reset", "??????"),
            ("/exit", "?? CLI"),
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
            # 回车优先选中当前下拉项（贴近常见 CLI 习惯）
            buf.apply_completion(buf.complete_state.current_completion)
            return
        buf.validate_and_handle()

    return kb



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
):
    status_text = "[bold cyan]Thinking... searching Wiki and calling model[/bold cyan]"
    if mode == "general_only":
        status_text = "[bold cyan]Thinking... calling model directly (general_only)[/bold cyan]"
    elif mode == "wiki_only":
        status_text = "[bold cyan]Thinking... searching Wiki only (wiki_only)[/bold cyan]"
    with console.status(status_text, spinner="dots"):
        return agent.run(
            user_input,
            force_wiki=force_wiki,
            mode=mode,  # type: ignore[arg-type]
            code_context=code_context,
            response_mode=response_mode,  # type: ignore[arg-type]
            target_file=target_file,
            history=history,
        )


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


def _extract_image_fields(obj: object) -> tuple[list[str], list[str], list[str]]:
    urls: list[str] = []
    b64s: list[str] = []
    texts: list[str] = []

    def walk(x: object) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                lk = str(k).lower()
                if isinstance(v, str):
                    if lk in {"url", "image_url"} and (v.startswith("http://") or v.startswith("https://")):
                        urls.append(v)
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


def _save_image_result(raw_result: str, save_dir: str, prefix: str) -> tuple[list[str], list[str], str]:
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
    llm = build_llm()
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
    llm = build_llm()
    try:
        result = llm.image_generate(prompt=prompt, size=size)
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]{e}[/red]")
        return
    urls, saved_files, meta_file = _save_image_result(result, save_dir=save_dir, prefix=prefix)

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
    console.print("[bold]WikiCoder REPL started[/bold]. Type /help for commands.")

    show_trace = trace
    show_stream = stream
    session_mode = "auto"
    last_patch_file = ""
    last_patch_output = ""
    last_patch_allowed: set[str] | None = None
    last_backup_id = ""
    session_history: list[tuple[str, str]] = []

    while True:
        try:
            text = session.prompt()
        except (KeyboardInterrupt, EOFError):
            console.print("\nBye.")
            break

        cmd = text.strip()
        if not cmd:
            continue

        if cmd in {"/exit", "/quit"}:
            console.print("Bye.")
            break

        if cmd == "/help":
            console.print(
                "Commands:\n"
                "/sync\n"
                "/vaultpath <dir>\n"
                "/kbclear yes\n"
                "/kbclear all yes\n"
                "/mode auto|wiki_only|general_only\n"
                "/eval <cases.jsonl> [topk] [out.json]\n"
                "/regress <cases.jsonl> [topk] [out.json]\n"
                "/compare <baseline.json> <latest.json>\n"
                "/baseline <report.json> [baseline.json]\n"
                "/ask <query>\n"
                "/review <file> :: <query>\n"
                "/patch <file> :: <query>\n"
                "/patchm <file1,file2> :: <query>\n"
                "/preview\n"
                "/apply yes\n"
                "/backups\n"
                "/undo [backup_id]\n"
                "/structure\n"
                "/trace on|off\n"
                "/stream on|off\n"
                "/reset\n"
                "/exit"
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
            if val not in {"auto", "wiki_only", "general_only"}:
                console.print("[yellow]Usage: /mode auto|wiki_only|general_only[/yellow]")
                continue
            session_mode = val
            console.print(f"[cyan]session mode = {session_mode}[/cyan]")
            continue

        if cmd == "/reset":
            session_history = []
            console.print("[cyan]已清空会话上下文记忆。[/cyan]")
            continue

        remember_turn = False
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
            code_ctx = f"file: {file}\n```\\n{code}\\n```"
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
            code_ctx = f"file: {file}\n```\\n{code}\\n```"
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
            code_ctx = "\n\n".join(blocks)
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
            resp = _run_agent_with_thinking(
                agent,
                user_input=cmd,
                force_wiki=False,
                history=session_history,
                mode=session_mode,
            )
            remember_turn = True

        if show_trace:
            _print_trace(resp.thought, resp.actions)

        _stream_markdown(resp.output, enabled=show_stream)
        if remember_turn:
            session_history.append((cmd, resp.output))
            if len(session_history) > 12:
                session_history = session_history[-12:]
        if cmd.startswith("/patch ") or cmd.startswith("/patchm "):
            _print_patch_preview(resp.output)


if __name__ == "__main__":
    app()


def run_cli() -> None:
    """Console entry: start REPL directly with one command."""
    chat(trace=False, stream=False)
