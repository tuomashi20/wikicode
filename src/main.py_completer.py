import sys
import os
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings

class SlashCommandCompleter(Completer):
    def __init__(self):
        # 补全树 - 最终全量版 (对标 main.py 所有指令)
        self.cmd_tree = {
            "/kbpath": "设置知识库路径 (支持目录补全)",
            "/sync": "同步知识库 (同 /kbupdate)",
            "/kbupdate": "更新并同步本地知识库",
            "/kbclear": "清除知识库向量索引",
            "/kbbackups": "查看知识库备份列表",
            "/kbrestore": "从备份 ID 恢复知识库",
            "/memdraft": "整理会话记录为 Wiki 文档",
            "/md2canvas": "Markdown 转 Obsidian Canvas",
            "/undo": "撤销上一步 Wiki 写入",
            "/mode": {"plan": "规划模式", "build": "构建模式"},
            "/model": {
                "jiutian-lan-comv3": "九天-揽月 V3", 
                "gpt-4o": "GPT-4o", 
                "deepseek-chat": "DeepSeek",
                "claude-3-5-sonnet": "Claude 3.5 Sonnet"
            },
            "/resume": "恢复上次会话上下文",
            "/reset": "重置会话记忆",
            "/ask": "向 AI 发起提问 (参数: <问题>)",
            "/patch": "生成文件修改补丁 (参数: <文件>)",
            "/review": "审查代码/文档规范 (参数: <文件>)",
            "/pdf2md": "PDF 转 Markdown (支持文件补全)",
            "/docx2md": "Word 转 Markdown (支持文件补全)",
            "/xlsx2md": "Excel 转 Markdown",
            "/version": "查看系统版本信息",
            "/help": "查看完整命令手册",
            "/exit": "安全退出应用"
        }

    def get_completions(self, document: Document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"): return
        parts = text.split()
        
        # 1. 动态文件/目录补全 (针对路径指令)
        path_cmds = {"/pdf2md", "/docx2md", "/xlsx2md", "/kbpath", "/patch", "/review"}
        if len(parts) >= 1 and parts[0] in path_cmds:
            prefix = parts[0]
            current_arg = parts[1] if len(parts) > 1 and not text.endswith(" ") else ""
            try:
                import glob
                # 针对不同命令提供不同的联想策略
                if prefix == "/kbpath":
                    # 路径补全只联想目录
                    entries = glob.glob(f"{current_arg}*") + glob.glob(f"{current_arg}**/", recursive=True)
                    files = [e for e in entries if os.path.isdir(e)]
                else:
                    # 文件补全
                    ext_map = {"/pdf2md": ".pdf", "/docx2md": ".docx", "/xlsx2md": ".xlsx"}
                    ext = ext_map.get(prefix, "")
                    files = glob.glob(f"*{ext}") + glob.glob(f"**/*{ext}", recursive=True)
                
                for f in sorted(list(set(files))):
                    if f == current_arg: continue
                    if not current_arg or f.lower().startswith(current_arg.lower()):
                        yield Completion(f, start_position=-len(current_arg), display_meta="路径联想")
            except: pass
            return

        # 2. 级联补全 (Mode, Model)
        if len(parts) >= 1:
            root = parts[0]
            if root in self.cmd_tree and isinstance(self.cmd_tree[root], dict):
                sub_dict = self.cmd_tree[root]
                if len(parts) == 1 and text.endswith(" "):
                    for sub, desc in sub_dict.items():
                        yield Completion(sub, start_position=0, display_meta=desc)
                    return
                elif len(parts) > 1:
                    query = parts[1]
                    for sub, desc in sub_dict.items():
                        if sub == query: continue
                        if sub.startswith(query):
                            yield Completion(sub, start_position=-len(query), display_meta=desc)
                    return

        # 3. 基础一级命令补全
        if len(parts) <= 1 and not text.endswith(" "):
            query = parts[0] if parts else ""
            for cmd, info in self.cmd_tree.items():
                if cmd == query: continue
                if cmd.startswith(query):
                    desc = info if isinstance(info, str) else "包含子选项..."
                    yield Completion(cmd, start_position=-len(query), display_meta=desc)

def build_key_bindings() -> KeyBindings:
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()
    @kb.add("c-c")
    def _(event): event.app.exit()
    @kb.add("escape")
    def _(event):
        buf = event.app.current_buffer
        if buf.complete_state: buf.cancel_completion()
    @kb.add("enter")
    @kb.add(" ")
    def _(event):
        buf = event.app.current_buffer; key_name = str(event.key_sequence[0].key).lower()
        if buf.complete_state and buf.complete_state.current_completion:
            buf.apply_completion(buf.complete_state.current_completion)
            if "enter" in key_name: buf.validate_and_handle(); return
            if not buf.text.endswith(" "): buf.insert_text(" ")
            buf.start_completion(select_first=False); return
        if "enter" in key_name: buf.validate_and_handle()
        elif " " in key_name: buf.insert_text(" ")
    return kb
