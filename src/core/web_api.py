from __future__ import annotations
import json
import traceback
import threading
import queue
import asyncio
from typing import AsyncGenerator
import uuid
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
import yaml

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.utils.config import load_config, PROJECT_ROOT, DEFAULT_CONFIG_PATH, ensure_workspace
from src.core.wikicoder_engine import BuildAgent, BuildStep
from src.cli.commands_wiki import app as cli_app
from src.skills.wiki_skill import sync_kb, clear_kb, set_vault_path, get_structure
from src.skills.chat_archive_skill import archive_chat_to_md, mem_draft_archive
from src.core.constants import CORE_COMMANDS, get_command_list
# from src.core.business_ops import get_pure_business_graph, run_business_audit

app = FastAPI(title="WikiCoder API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

PENDING_CONFIRMATIONS = {}

# [关键修复]：启动即加载配置，确保数据库路径（vault_path）正确锚定
try:
    load_config()
except:
    pass

@app.post("/v1/archive")
async def archive_history(req: dict):
    history = req.get("history", [])
    ok, path = archive_chat_to_md(history)
    if ok:
        return {"status": "success", "output": f"✅ 对话已成功总结并存档到：\n`{path}`"}
    return {"status": "error", "message": path}

class ChatRequest(BaseModel):
    query: str
    history: list = []
    cwd: str = "."
    mode: str = "chat"
    agent_search_limit: int | None = None
    rag_filename_boost_terms: list | None = None
    report_template: str | None = None

def format_history(history):
    result = []
    for h in history:
        if isinstance(h, dict):
            result.append((h.get("q", ""), h.get("a", "")))
        elif isinstance(h, (list, tuple)) and len(h) >= 2:
            result.append((h[0], h[1]))
    return result

@app.post("/v1/chat")
async def chat(chat_request: ChatRequest, request: Request):
    try:
        config = load_config()
        agent_build = BuildAgent(config)
        history_tuples = format_history(chat_request.history)
        
        async def event_generator():
            status_queue = queue.Queue()
            agent_done = False
            
            def _on_step(step: BuildStep) -> bool:
                # 危险操作拦截逻辑
                is_dangerous = step.action_type in ["shell", "python", "edit_file", "write_file"]
                confirm_id = str(uuid.uuid4()) if is_dangerous else ""
                
                # [Sync] 交互式请示识别
                is_interaction = step.action_type == "ask_user"
                interaction_data = {}
                if is_interaction:
                    try:
                        interaction_data = json.loads(step.action_input)
                    except:
                        interaction_data = {"question": step.action_input}

                status_queue.put({
                    "type": "step", 
                    "thought": step.thought,
                    "action_type": step.action_type,
                    "action_input": step.action_input,
                    "tasks": step.tasks,
                    "require_confirm": is_dangerous or is_interaction,
                    "confirm_id": confirm_id,
                    "interaction": interaction_data # 透传结构化的问题与选项
                })
                
                if is_dangerous:
                    event = threading.Event()
                    PENDING_CONFIRMATIONS[confirm_id] = {"event": event, "approved": False}
                    event.wait() 
                    result = PENDING_CONFIRMATIONS.pop(confirm_id, {"approved": False})
                    return result["approved"]
                return True

            def _on_log(content: str) -> bool:
                # 增强日志识别
                if "正在同步内置长程记忆" in content or "WikiCoder [AGENT]" in content:
                    status_queue.put({"type": "step", "thought": content, "action_type": "info"})
                else:
                    status_queue.put({"type": "log", "content": content})
                return True

            def _run_agent():
                nonlocal agent_done
                try:
                    out = agent_build.run(
                        chat_request.query, 
                        history_tuples, 
                        on_step=_on_step, 
                        on_log=_on_log, 
                        mode=chat_request.mode,
                        report_template=chat_request.report_template
                    )
                    status_queue.put({"type": "done", "output": out})
                except Exception as e:
                    status_queue.put({"type": "error", "content": str(e)})
                finally:
                    agent_done = True

            status_queue.put({"type": "step", "thought": "🚀 正在启动 WikiCoder 智能引擎...", "action_type": "init", "tasks": []})
            threading.Thread(target=_run_agent, daemon=True).start()

            yield f"data: {json.dumps({'status': 'WikiCoder 引擎已就绪...'}, ensure_ascii=False)}\n\n"

            last_act = asyncio.get_running_loop().time()
            try:
                while not agent_done or not status_queue.empty():
                    if await request.is_disconnected():
                        agent_build.interrupt_signal = True
                        break
                    try:
                        item = status_queue.get_nowait()
                        last_act = asyncio.get_running_loop().time()
                        if item["type"] == "step":
                            yield f"data: {json.dumps({'status': item['thought'], 'tasks': item.get('tasks', []), 'action_type': item.get('action_type', ''), 'require_confirm': item.get('require_confirm', False), 'confirm_id': item.get('confirm_id', '')}, ensure_ascii=False)}\n\n"
                            await asyncio.sleep(0) 
                        elif item["type"] == "log":
                            yield f"data: {json.dumps({'output': item['content']}, ensure_ascii=False)}\n\n"
                            await asyncio.sleep(0) 
                        elif item["type"] == "done":
                            is_interrupted = item['output'] == "__INTERRUPTED_WAITING_USER__"
                            yield f"data: {json.dumps({
                                'thought': '等待用户决策' if is_interrupted else '任务完成', 
                                'output': item['output'],
                                'is_interrupted': is_interrupted
                            }, ensure_ascii=False)}\n\n"
                            await asyncio.sleep(0) 
                            agent_done = True
                        elif item["type"] == "error":
                            yield f"data: {json.dumps({'error': item['content']}, ensure_ascii=False)}\n\n"
                            agent_done = True
                            break
                    except queue.Empty:
                        now = asyncio.get_running_loop().time()
                        elapsed = int(now - last_act)
                        # 只有在等待超过 2 秒，且距离上次发送状态消息超过 2 秒时才发送
                        if elapsed >= 2 and (not hasattr(event_generator, "last_status_time") or now - event_generator.last_status_time >= 2):
                            event_generator.last_status_time = now
                            yield f"data: {json.dumps({'status': f'🧠 WikiCoder 正在全速构思中 (已耗时 {elapsed}s)...'}, ensure_ascii=False)}\n\n"
                        await asyncio.sleep(0.05)
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/confirm")
async def confirm_action(req: dict):
    cid = req.get("confirm_id")
    if cid in PENDING_CONFIRMATIONS:
        PENDING_CONFIRMATIONS[cid]["approved"] = req.get("approved", False)
        PENDING_CONFIRMATIONS[cid]["event"].set()
        return {"status": "success"}
    return {"status": "error"}

@app.get("/v1/commands")
async def get_commands():
    return get_command_list()

@app.get("/v1/templates")
async def get_templates():
    """获取所有可用的报告模板清单及其默认配置"""
    template_dir = PROJECT_ROOT / "src" / "templates" / "reports"
    templates = []
    if template_dir.exists():
        # 扫描目录下所有的 md 文件
        for f in template_dir.glob("*.md"):
            # 这里的 name 转换一下，比如 business_audit -> Business Audit
            display_name = f.stem.replace("_", " ").title()
            templates.append({"id": f.name, "name": display_name})
    
    # 获取当前配置中的默认模板
    config = load_config()
    default_template = "business_audit.md"
    try:
        if hasattr(config, "wiki_strategy"):
            default_template = getattr(config.wiki_strategy, "report_template", default_template)
        else:
            default_template = config.get("wiki_strategy", {}).get("report_template", default_template)
    except:
        pass
        
    return {
        "templates": templates,
        "default": default_template
    }

# --- [NEW] 业务运营分析接口 (WebUI 专用) ---
# @app.get("/v1/ops/graph")
# async def get_ops_graph():
#     """获取全量业务逻辑图谱"""
#     return get_pure_business_graph()
# 
# @app.get("/v1/ops/audit")
# async def get_ops_audit():
#     """获取业务违规审计报表"""
#     return run_business_audit()

@app.post("/v1/exec")
async def execute_command(req: dict):
    full_cmd = req.get("command", "").strip()
    if not full_cmd: return {"status": "error", "output": "无效命令"}
    parts = full_cmd.split()
    cmd_name = parts[0].replace("/", "")
    arg = " ".join(parts[1:]) if len(parts) > 1 else ""
    history = req.get("history", [])
    
    try:
        # [1] 核心业务逻辑
        if cmd_name == "sync":
            result = sync_kb()
            return {"status": "success", "output": f"✅ **同步完成**\n- 修改文件: `{result.get('files', 0)}`"}
            
        elif cmd_name == "structure":
            items = get_structure()
            if not items: return {"status": "success", "output": "📭 当前知识库索引为空。"}
            table = "| 文件名 | 切片数 |\n| :--- | :--- |\n"
            for it in items: table += f"| {it['parent_file']} | {it['chunk_count']} |\n"
            return {"status": "success", "output": f"📊 **知识库物理结构**:\n\n{table}"}
            
        elif cmd_name == "kbclear":
            msgs = clear_kb(all_data=True)
            return {"status": "success", "output": "### 🧹 知识库清理报告\n\n" + "\n".join([f"- {m}" for m in msgs])}

        elif cmd_name == "kbbackups":
            from src.skills.kb_backup_skill import get_backups
            items = get_backups(limit=10)
            if not items: return {"status": "success", "output": "📭 暂无备份记录。"}
            lines = [f"- **{it['id']}** ({it['created_at']})" for it in items]
            return {"status": "success", "output": "📅 **知识库备份列表**:\n\n" + "\n".join(lines)}

        elif cmd_name == "kbsave":
            from src.skills.kb_backup_skill import create_backup
            bid, msgs = create_backup(name=arg or None)
            return {"status": "success" if bid else "error", "output": f"✅ **备份创建成功**: `{bid}`\n\n" + "\n".join(msgs)}

        elif cmd_name == "kbrestore":
            if not arg: return {"status": "error", "output": "❌ 请指定要恢复的备份 ID"}
            from src.skills.kb_backup_skill import restore_backup_by_id
            ok, msgs = restore_backup_by_id(arg)
            return {"status": "success" if ok else "error", "output": "\n".join(msgs)}

        elif cmd_name in ["vaultpath", "kbpath"]:
            if not arg: return {"status": "error", "output": "❌ 请指定路径"}
            ok, msg = set_vault_path(arg)
            return {"status": "success" if ok else "error", "output": msg}

        elif cmd_name == "status":
            cfg = load_config()
            out = f"📊 **运行状态**\n- 模型: `{cfg.llm.model}`\n- 知识库: `{cfg.wiki_strategy.vault_path}`\n- 工作目录: `{os.getcwd()}`"
            return {"status": "success", "output": out}

        elif cmd_name == "help":
            from src.core.web_api import get_commands
            cmds = await get_commands()
            lines = [f"- **{c['name']}**: {c['desc']}" for c in cmds]
            return {"status": "success", "output": "📖 **WikiCoder 指令手册**\n\n" + "\n".join(lines)}

        elif cmd_name == "exit":
            return {"status": "success", "output": "👋 感谢使用 WikiCoder！您可以直接点击侧边栏顶部的关闭图标或切换到其他插件。"}

        elif cmd_name == "version":
            return {"status": "success", "output": "🚀 **WikiCoder Pro**\n- 核心引擎: `v4.2.0` (Interactive Core)\n- 适配层: `v2.1.0` (Obsidian Ready)"}

        elif cmd_name == "archive":
            if not history: return {"status": "error", "output": "❌ 归档失败：对话历史为空。"}
            ok, path = archive_chat_to_md(history)
            return {"status": "success" if ok else "error", "output": f"✅ 已为您生成正式归档：\n`{path}`" if ok else f"❌ 归档失败: {path}"}

        # [2] 工具类代理 (仅保持文档转换等原子工具)
        cli_cmds = ["xlsx2md", "pdf2md", "docx2md", "version", "undo"]
        if cmd_name in cli_cmds:
            executable = sys.executable
            main_script = str(PROJECT_ROOT / "src" / "main.py")
            cli_args = [executable, main_script, cmd_name]
            if arg: cli_args.extend(parts[1:])
            res = subprocess.run(cli_args, capture_output=True, text=True, cwd=str(PROJECT_ROOT), encoding="utf-8")
            output = res.stdout if res.stdout else res.stderr
            import re
            output = re.sub(r"\x1b\[[0-9;]*m", "", output)
            return {"status": "success" if res.returncode == 0 else "error", "output": output.strip()}

        # [3] 特殊状态同步
        if cmd_name == "resume":
            from src.cli.repl import _load_session_state
            h, m = _load_session_state()
            if h: return {"status": "success", "output": f"✅ 已成功恢复会话记录。", "history": h, "mode": m}
            return {"status": "error", "output": "❌ 未发现可恢复的会话记录。"}

        elif cmd_name == "mode":
            if arg in ["chat", "agent"]: return {"status": "success", "output": f"🔄 **模式已切换**: `{arg.upper()}`", "mode": arg}
            return {"status": "error", "output": "❌ 请指定模式"}

        elif cmd_name == "model":
            if not arg: return {"status": "error", "output": "❌ 请指定模型名称"}
            if DEFAULT_CONFIG_PATH.exists():
                data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")) or {}
                data.setdefault("llm", {})["model"] = arg
                DEFAULT_CONFIG_PATH.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
                return {"status": "success", "output": f"✅ 模型已切换为: `{arg}`"}
            return {"status": "error", "output": "❌ 配置文件丢失"}

        return {"status": "error", "output": f"❓ 未知或暂未映射的命令 `{full_cmd}`"}
    except Exception as e:
        return {"status": "error", "output": f"❌ 执行失败: {str(e)}"}

def start_server(host="127.0.0.1", port=8000):
    import uvicorn
    uvicorn.run(
        app, 
        host=host, 
        port=port, 
        log_level="warning", # 核心修复：防止干扰终端渲染
        access_log=False      # 核心修复：禁止输出访问日志
    )
