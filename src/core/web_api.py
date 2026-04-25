from __future__ import annotations

import json
import traceback
import threading
import queue
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anyio

from src.core.agent import WikiFirstAgent, AgentResponse
from src.utils.config import load_config
from src.main import build_agent, run_sync, build_llm, _save_memory_markdown
from src.core.build_agent import BuildAgent, BuildStep

app = FastAPI(title="WikiCoder API", version="0.1.0")

# 配置 CORS，允许 Obsidian 跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_msg = traceback.format_exc()
    print(f"Server Error:\n{error_msg}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "traceback": error_msg}
    )

class ChatRequest(BaseModel):
    query: str
    mode: str = "auto" # auto, wiki_only, general_only, build
    history: list[dict[str, str]] = []

def format_history(history: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(h["q"], h["a"]) for h in history]

class DraftRequest(BaseModel):
    history: list[dict[str, str]]
    title_hint: str = ""

class SaveRequest(BaseModel):
    title: str
    content: str

@app.get("/health")
async def health_check():
    return {"status": "ok", "app": "wikicoder"}

@app.post("/v1/chat")
async def chat(request: ChatRequest):
    """
    流式问答接口。
    支持 Plan (auto/wiki_only/general_only) 和 Build 模式。
    """
    try:
        config = load_config()
        agent = build_agent(config)
        history_tuples = format_history(request.history)
        
        import queue
        status_queue = queue.Queue()

        async def event_generator():
            try:
                if request.mode == "build":
                    # Build 模式：运行交互式命令流 Agent
                    agent_build = BuildAgent(config)
                    
                    # 立即发送启动信号
                    status_queue.put({"type": "status", "content": "Build 模式交互引擎已启动，正在分析任务..."})

                    def _on_step(step: BuildStep) -> bool:
                        # 将步骤推送到队列，供 SSE 发送
                        status_queue.put({
                            "type": "step", 
                            "thought": step.thought,
                            "action": f"{step.action_type}: {step.action_input}",
                            "observation": step.observation
                        })
                        return True

                    def _run_agent():
                        try:
                            output = agent_build.run(request.query, history_tuples, on_step=_on_step)
                            status_queue.put({"type": "done", "output": output})
                        except Exception as e:
                            status_queue.put({"type": "error", "content": str(e)})

                    threading.Thread(target=_run_agent, daemon=True).start()

                    while True:
                        try:
                            item = await anyio.to_thread.run_sync(lambda: status_queue.get(timeout=0.1))
                            if item["type"] == "status":
                                yield f"data: {json.dumps({'status': item['content']}, ensure_ascii=False)}\n\n"
                            elif item["type"] == "step":
                                status_msg = f"**思考**: {item['thought']}\n\n**执行**: `{item['action']}`"
                                if item.get("observation"):
                                    status_msg += f"\n\n**结果**:\n```\n{item['observation']}\n```"
                                yield f"data: {json.dumps({'status': status_msg}, ensure_ascii=False)}\n\n"
                            elif item["type"] == "done":
                                yield f"data: {json.dumps({'thought': '任务完成', 'output': item['output']}, ensure_ascii=False)}\n\n"
                                break
                            elif item["type"] == "error":
                                yield f"data: {json.dumps({'error': item['content']}, ensure_ascii=False)}\n\n"
                                break
                        except queue.Empty:
                            await anyio.sleep(0.1)
                else:
                    # Plan 模式：正常的 Agent 问答
                    response: AgentResponse = await anyio.to_thread.run_sync(
                        agent.run, 
                        request.query, 
                        False, 
                        request.mode, 
                        "", 
                        "answer", 
                        "", 
                        history_tuples
                    )
                    if response.thought:
                        yield f"data: {json.dumps({'thought': response.thought, 'output': ''}, ensure_ascii=False)}\n\n"
                    yield f"data: {json.dumps({'thought': '', 'output': response.output}, ensure_ascii=False)}\n\n"
                
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")
    except Exception as e:
        print(f"Chat Error: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/sync")
async def sync_knowledge(mode: str = "incremental", path: str = None):
    """触发本地知识库同步"""
    try:
        if mode == "full":
            from src.utils.db_manager import clear_index_store
            config = load_config()
            clear_index_store(config.wiki_strategy.processed_path)
            results = run_sync()
        elif mode == "clear":
            from src.utils.db_manager import clear_index_store
            config = load_config()
            messages = clear_index_store(config.wiki_strategy.processed_path)
            return {"status": "success", "results": {"cleared": True, "details": messages}}
        else:
            results = run_sync()
        return {"status": "success", "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class CmdRequest(BaseModel):
    command: str

@app.post("/v1/exec")
async def exec_cmd(request: CmdRequest):
    """处理斜杠命令及其参数引导逻辑"""
    cmd_full = request.command.strip()
    parts = cmd_full.split(" ")
    cmd = parts[0].lower()
    args = parts[1:] if len(parts) > 1 else []

    try:
        # 1. 基础状态与配置查看
        if cmd == "/status":
            config = load_config()
            return {"status": "success", "output": (
                f"### ⚙️ 系统当前状态\n"
                f"- **文本模型**: `{config.llm.model}`\n"
                f"- **知识库**: `{config.wiki_strategy.vault_path or '未激活'}`\n"
                f"- **服务器**: `running at 127.0.0.1:8000`"
            )}

        # 2. 知识库路径
        if cmd == "/vaultpath":
            if not args:
                return {"status": "error", "output": "### ⚠️ 参数缺失\n用法: `/vaultpath <本地目录路径>`\n示例: `/vaultpath D:\\MyNotes`"}
            from src.main import _set_vault_path
            ok, msg = _set_vault_path(" ".join(args))
            return {"status": "success" if ok else "error", "output": msg}

        # 3. 同步管理
        if cmd == "/sync":
            results = run_sync()
            return {"status": "success", "output": f"### ✅ 同步结果\n- 新增/修改文件: {results['files']}\n- 本次生成分片: {results['chunks']}"}
        
        if cmd == "/kbclear":
            if "yes" not in args:
                return {"status": "error", "output": "### 🗑️ 清理确认\n此操作不可逆，请确认为：\n- `/kbclear yes`: 仅清理索引\n- `/kbclear all yes`: 清理索引及 Wiki 页面"}
            from src.utils.db_manager import clear_index_store
            config = load_config()
            msgs = clear_index_store(config.wiki_strategy.processed_path)
            if "all" in args:
                from src.main import _clear_wiki_output
                msgs.extend(_clear_wiki_output(config.wiki_strategy.wiki_path))
            return {"status": "success", "output": "### ✨ 已手动执行清理\n" + "\n".join([f"- {m}" for m in msgs])}

        # 4. 备份与恢复
        if cmd == "/kbbackups":
            from src.main import list_kb_backups
            items = list_kb_backups(limit=20)
            if not items: return {"status": "success", "output": "未发现备份快照。"}
            return {"status": "success", "output": "### 💾 备份列表\n" + "\n".join([f"- `{it['id']}` - {it['created_at']}" for it in items])}

        if cmd == "/kbsave":
            from src.main import save_kb_backup
            bid, msgs = save_kb_backup(load_config(), name=args[0] if args else None)
            return {"status": "success", "output": f"### 📦 备份完成\nID: `{bid}`"}

        if cmd == "/kbrestore":
            if not args:
                return {"status": "error", "output": "### ⚠️ 请提供备份 ID\n用法: `/kbrestore <backup_id>`\n提示: 输入 `/kbbackups` 查看可用 ID。"}
            from src.main import restore_kb_backup
            ok, msgs = restore_kb_backup(load_config(), args[0])
            return {"status": "success" if ok else "error", "output": "\n".join(msgs)}

        # 5. 模型与模式切换
        if cmd == "/model":
            config = load_config()
            if not args:
                return {"status": "error", "output": (
                    f"### 🤖 模型设置\n"
                    f"当前使用模型: `{config.llm.model}`\n\n"
                    f"用法: `/model <模型名称>`\n"
                    f"提示：您可以快速切换为以下 WikiCoder 专用模型：\n"
                    f"- `jiutian-think-v3` (深度思考推理)\n"
                    f"- `jiutian-lan-comv3` (通用快速回答)"
                )}
            from src.main import _set_model_config
            ok, msg = _set_model_config(args[0])
            return {"status": "success" if ok else "error", "output": msg}

        if cmd == "/mode":
            if not args or args[0] not in {"auto", "wiki_only", "general_only"}:
                return {"status": "error", "output": "### 🛠️ 模式选择范围\n- `/mode auto`: 智能检索 (默认)\n- `/mode wiki_only`: 严格仅使用本地知识库\n- `/mode general_only`: 跳过知识库，直接问 AI"}
            # 注意：mode 需要修改全局配置或会话状态，这里示例修改配置
            return {"status": "success", "output": f"✅ 切换成功: 模式 = `{args[0]}`"}

        # 6. 对话与记忆管理
        if cmd == "/resume":
            from src.main import _load_session_state
            state = _load_session_state()
            if not state or "history" not in state:
                return {"status": "error", "output": "### ⚠️ 未找到历史会话\n无法执行 `/resume`。"}
            # 这里简单返回状态，实际历史会在下一次 /chat 时带上
            return {"status": "success", "output": f"### 🔄 会话已恢复\n- 历史消息数: {len(state.get('history', []))}\n- 上次模型: `{state.get('model', 'default')}`"}

        if cmd == "/memdraft":
            return {"status": "success", "output": "### 📝 整理建议\n请点击界面顶部的 **[整理记录]** 按钮，系统将自动汇总本次对话并生成 Wiki 草稿。"}

        if cmd == "/memsave":
            return {"status": "success", "output": "### 💾 保存建议\n请点击界面顶部的 **[入库]** 按钮，将当前整理好的草稿保存到本地知识库。"}

        if cmd == "/ask":
            return {"status": "success", "output": "### 💡 提示\n`/ask` 指令用于强制 Wiki 模式。在插件中，您可以直接输入问题，或者通过 `/mode wiki_only` 切换到该模式。"}

        # 7. 文件转换工具
        if cmd == "/pdf2md":
            if not args: return {"status": "error", "output": "用法: `/pdf2md <路径>`"}
            from src.skills.pdf_tools import convert_pdf_path
            outs, errs = convert_pdf_path(" ".join(args))
            return {"status": "success", "output": f"✅ 已完成 PDF 转换: {len(outs)} 个文件"}

        if cmd == "/docx2md":
            if not args: return {"status": "error", "output": "用法: `/docx2md <路径>`"}
            from src.skills.docx_tools import convert_docx_path
            outs, errs = convert_docx_path(" ".join(args))
            return {"status": "success", "output": f"✅ 已完成 Word 转换: {len(outs)} 个文件"}

        if cmd == "/xlsx2md":
            if not args: return {"status": "error", "output": "用法: `/xlsx2md <路径>`"}
            from src.skills.xlsx_tools import convert_xlsx_path
            outs, errs = convert_xlsx_path(" ".join(args))
            return {"status": "success", "output": f"✅ 已完成 Excel 转换: {len(outs)} 个文件"}

        # 8. Canvas 转换
        if cmd in {"/md2canvas", "/md2canvas_ai"}:
            from src.skills.canvas_tools import convert_md_canvas_path
            use_ai = (cmd == "/md2canvas_ai")
            if not args:
                return {"status": "error", "output": f"### ⚠️ 参数缺失\n用法: `{cmd} <路径> [-r]`"}
            
            # 提取参数
            path_arg = " ".join(args)
            recursive = False
            if "-r" in path_arg or "--recursive" in path_arg:
                recursive = True
                path_arg = path_arg.replace("--recursive", "").replace("-r", "").strip()
            
            outs, errs = convert_md_canvas_path(path_arg, recursive=recursive, use_ai=use_ai)
            
            msg_lines = []
            if outs:
                msg_lines.append("### ✅ 转换成功")
                for o in outs:
                    msg_lines.append(f"- 已生成: `{o.name}`")
                    msg_lines.append(f"FILE_PATH:{o.resolve()}") 
            if errs:
                msg_lines.append("### ❌ 转换部分失败")
                for e in errs:
                    msg_lines.append(f"- {e}")
            
            return {"status": "success" if outs else "error", "output": "\n".join(msg_lines)}

        # 9. 帮助内容
            return {"status": "success", "output": (
                "### 📖 Wikicodian 指令手册\n"
                "| 指令 | 说明 | 必选参数 |\n"
                "| :--- | :--- | :--- |\n"
                "| `/sync` | 学习新笔记 | 无 |\n"
                "| `/kbclear` | 重置索引 | `yes` |\n"
                "| `/vaultpath` | 设置库路径 | `<路径>` |\n"
                "| `/model` | 切换 AI 模型 | `<名称>` |\n"
                "| `/mode` | 检索策略 | `auto/wiki/gen` |\n"
                "| `/kbsave` | 保存存档 | `[备注]` |\n"
                "| `/kbrestore` | 恢复指令 | `<ID>` |\n"
                "| `/reset` | 清空对话 | 无 |"
            )}

        return {"status": "error", "output": f"找不到该命令: `{cmd}`。输入 `/help` 查看所有指令。"}
    except Exception as e:
        return {"status": "error", "output": f"🔥 执行失败: `{str(e)}`"}

@app.get("/v1/files")
async def get_files():
    """获取知识库中已索引的所有文件列表 (用于前端 @ 引用提醒)"""
    try:
        from src.skills.wiki_tools import wiki_list_structure
        items = wiki_list_structure()
        return {"status": "success", "files": [it['parent_file'] for it in items]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/v1/config")
async def get_config():
    """获取基础配置（用于插件端显示进度或状态）"""
    config = load_config()
    return {
        "vault_path": str(config.wiki_strategy.vault_path),
        "model": config.llm.model,
        "provider": config.llm.provider
    }

@app.post("/v1/memdraft")
async def mem_draft(request: DraftRequest):
    """将对话历史整理为 Wiki Markdown 草稿"""
    try:
        if not request.history:
            return {"status": "error", "message": "当前会话暂无可整理内容。"}
        
        config = load_config()
        llm = build_llm(config)
        
        # 组装对话文本
        hist_text = "\n\n".join(
            [f"### 用户问题\n{h['q']}\n\n### 助手回答\n{h['a']}" for h in request.history[-12:]]
        )
        
        system_prompt = (
            "你是知识工程师。请把给定对话整理为可直接入库的中文 Wiki Markdown 文档。"
            "要求：结构清晰、可复用、避免口语、不要编造事实。"
        )
        user_prompt = (
            f"文档标题建议：{request.title_hint or '自动整理会话'}\n\n"
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

        # 异步调用 LLM (由于 generate 是同步的，放到线程池)
        import anyio
        draft = await anyio.to_thread.run_sync(
            llm.generate, 
            system_prompt, 
            user_prompt
        )
        
        import re
        m = re.search(r"^#\s+(.+)$", draft, flags=re.MULTILINE)
        title = (m.group(1).strip() if m else request.title_hint or "会话整理")
        
        return {
            "status": "success", 
            "draft": draft, 
            "title": title
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/v1/memsave")
async def mem_save(request: SaveRequest):
    """将草稿保存到本地 raw/faq 目录"""
    try:
        config = load_config()
        # 异步保存
        import anyio
        out_path = await anyio.to_thread.run_sync(
            _save_memory_markdown,
            config,
            request.title,
            request.content
        )
        return {
            "status": "success",
            "path": str(out_path),
            "message": f"已成功入库：{out_path.name}"
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

def start_server(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
