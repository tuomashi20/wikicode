from typing import Any, Dict, Callable, List
import subprocess

class Toolbox:
    """[WikiCoder 插件化工具箱] 支持交互式提权与安全确认"""
    
    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register_tool(self, name: str, func: Callable, desc: str):
        self._tools[name] = {"func": func, "desc": desc}

    def get_tool_descriptions(self, filter_list: List[str] = None) -> str:
        lines = []
        for name, info in self._tools.items():
            if filter_list and name not in filter_list: continue
            lines.append(f"- {name}: {info['desc']}")
        return "\n".join(lines)

    def execute(self, name: str, params: Any, engine: Any = None) -> tuple[str, Any]:
        if name not in self._tools: return f"错误: 未找到工具 '{name}'", None
        try:
            return self._tools[name]["func"](params, engine=engine)
        except Exception as e: return f"执行异常: {str(e)}", None

toolbox = Toolbox()

# --- 核心技能包装 ---

def _wrap_ask_user(p, engine=None):
    """[核心交互] 工业级交互：支持快捷键提示与高亮"""
    if not isinstance(p, dict): p = {"question": str(p)}
    question = p.get("question", "")
    options = p.get("options", [])
    
    # 自动映射快捷键
    shortcuts = {"是": "y", "否": "n", "全部": "a", "继续": "y", "终止": "n", "Yes": "y", "No": "n", "All": "a"}
    decorated_opts = []
    for opt in options:
        sc = shortcuts.get(opt, "")
        decorated_opts.append(f"{opt}({sc})" if sc else opt)
        
    # 返回中断信号，附带装饰后的信息
    return "__ASK_USER_INTERRUPT__", {
        "question": question, 
        "options": options,
        "hint": "/".join(decorated_opts) if decorated_opts else "请输入内容"
    }

def _wrap_wiki_search(p, engine=None):
    from src.skills.wiki_tools import wiki_search_v2, wiki_read_chunk
    query = p.get("query") if isinstance(p, dict) else str(p)
    res, _ = wiki_search_v2(query, limit=5, llm=getattr(engine, 'llm', None))
    obs = "\n".join([f"文件路径: {r['parent_file']}\n内容摘要:\n{wiki_read_chunk(r['chunk_id'])}" for r in res]) if res else "未找到相关规约。"
    return obs, res

def _wrap_read_file(p, engine=None):
    from src.skills.code_tools import read_file
    if isinstance(p, dict):
        path = p.get("path") or p.get("file_path") or p.get("file") or p.get("filename")
        query = p.get("query") or p.get("keyword") or p.get("section")
        start_line = p.get("start_line") or p.get("start")
        end_line = p.get("end_line") or p.get("end")
        # 确保转为整数
        start_line = int(start_line) if start_line is not None else None
        end_line = int(end_line) if end_line is not None else None
    else:
        path = str(p)
        query = start_line = end_line = None
    
    if not path:
        return "错误: 未提供文件路径参数。", None
        
    res = read_file(path, query=query, start_line=start_line, end_line=end_line)
    msg = f"内容读取 ({path})"
    if start_line is not None:
        msg += f" [第 {start_line} 至 {end_line if end_line else '末尾'} 行]"
    elif query:
        msg += f" [基于关键词 '{query}' 的定位片段]"
    
    if len(res) > 12000:
        return f"{msg} [前12000字符]:\n{res[:12000]}\n\n⚠️ 内容过长已截断。", res
    return f"{msg}:\n{res}", res

def _wrap_write_file(p, engine=None):
    from src.skills.code_tools import write_file
    if isinstance(p, dict):
        path = p.get("path") or p.get("file_path") or p.get("file") or p.get("filename")
        content = p.get("content") or p.get("text") or ""
    else:
        # 如果不是字典，可能直接传的是路径，这种场景较少但需防御
        path = str(p)
        content = ""
    # [安全审计] 建议模型在调用此工具前先用 ask_user 获准
    write_file(path, content)
    return f"✅ 成功写入本地文件: {path}", None

def _wrap_run_command(p, engine=None):
    """[交互式提权] 自动处理 UOS/Linux 的 sudo 密码请求"""
    cmd = p.get("command") or p.get("cmd") if isinstance(p, dict) else str(p)
    
    # 1. 尝试直接执行
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    
    # 2. 检查是否需要 sudo 密码 (UOS/Linux 特征)
    if "sudo" in cmd and proc.returncode != 0 and ("password" in proc.stderr.lower() or "password" in proc.stdout.lower()):
        # 自动调起 ask_user 获取密码
        pwd_obs, pwd = _wrap_ask_user({"question": f"执行 '{cmd}' 需要 sudo 权限，请输入密码:"}, engine)
        if pwd:
            # 使用 -S 参数从 stdin 读取密码重试
            retry_cmd = f"echo '{pwd}' | sudo -S {cmd.replace('sudo ', '')}"
            proc = subprocess.run(retry_cmd, shell=True, capture_output=True, text=True, timeout=30)
            return f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}", None
            
    return f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}", None

def _wrap_read_excel(p, engine=None):
    from src.skills.code_tools import read_excel
    if isinstance(p, dict):
        path = p.get("path") or p.get("file_path") or p.get("file") or p.get("filename")
    else:
        path = str(p)
    res = read_excel(path=path)
    return f"Excel 读取结果:\n{res}", res

def _wrap_python_repl(p, engine=None):
    code = p.get("code") or p.get("command") if isinstance(p, dict) else str(p)
    if not engine or not hasattr(engine, "interpreter"):
        return "错误: 解释器未就绪。", None
    return engine.interpreter.execute(code), None

# --- [内置 gbrain 记忆引擎] ---

def _wrap_gbrain_put_page(p, engine=None):
    from src.core.memory.manager import memory_manager
    slug = p.get("slug")
    content = p.get("content")
    title = p.get("title")
    if not slug or not content:
        return "错误: 缺少 slug 或 content 参数。", None
    res = memory_manager.put_page(slug, content, title=title)
    return res, None

def _wrap_gbrain_get_page(p, engine=None):
    from src.core.memory.manager import memory_manager
    slug = p.get("slug") if isinstance(p, dict) else str(p)
    if not slug:
        return "错误: 缺少 slug 参数。", None
    res = memory_manager.get_page(slug)
    return res, None

def _wrap_gbrain_search(p, engine=None):
    from src.core.memory.manager import memory_manager
    query = p.get("query") if isinstance(p, dict) else str(p)
    if not query:
        return "错误: 缺少 query 参数。", None
    res = memory_manager.search_pages(query)
    return res, None

def _wrap_gbrain_list_pages(p, engine=None):
    from src.core.memory.manager import memory_manager
    res = memory_manager.list_pages()
    return res, None

# --- 注册 ---
toolbox.register_tool("ask_user", _wrap_ask_user, "【核心交互】向用户征询。参数：question(必填), options(可选，如['是','否'])。用于决策确认或补充信息。")
toolbox.register_tool("wiki_search", _wrap_wiki_search, "【找文件/看摘要】通过关键词查询业务规约。返回文件路径及片段摘要。找到目标文件后，请务必切换到 read_file 进行全文或章节研读。")
toolbox.register_tool("read_excel", _wrap_read_excel, "【数据透视】读取并分析 Excel 数据内容。它会返回列名摘要和数据样例。获取列名后，请优先考虑使用 write_file 生成 Python 脚本进行自动化分类汇总统计。")
toolbox.register_tool("read_file", _wrap_read_file, "【读条款/看细节】读取本地文件。支持 query(关键词定位) 或 start_line/end_line(物理翻页)。当 wiki_search 锁定文件后，用此工具研读具体章节。")
toolbox.register_tool("write_file", _wrap_write_file, "【工程化核心】向当前目录写入本地文件。这是实现复杂逻辑（如 Excel 汇总统计）的首选方案：先根据 read_excel 的反馈生成统计脚本 (.py)，再通过 run_command 执行该脚本。")
toolbox.register_tool("python_repl", _wrap_python_repl, "【临时调试】持久化 Python 内核执行。仅建议用于 10 行以内的临时验证或变量检查。严禁使用此工具执行大规模数据处理，请改用 write_file 生成脚本。")
toolbox.register_tool("run_command", _wrap_run_command, "【敏感】执行终端命令。可用于运行 Python 脚本 (python xxx.py)。支持 sudo 交互。")

# 注册内置记忆工具 (对齐原 gbrain 接口)
toolbox.register_tool("gbrain_put_page", _wrap_gbrain_put_page, "【长期记忆：存入】保存或更新用户的偏好、身份、项目共识或重要知识点。参数：slug(唯一标识, 如'personal_profile'), content(详细内容), title(可选标题)。")
toolbox.register_tool("gbrain_get_page", _wrap_gbrain_get_page, "【长期记忆：提取】根据 slug 调阅特定的长期记忆页面。常用于启动时加载用户画像。")
toolbox.register_tool("gbrain_search", _wrap_gbrain_search, "【长期记忆：模糊搜索】当本地规约找不到答案时，通过关键词在长期记忆库中进行语义检索。")
toolbox.register_tool("gbrain_list_pages", _wrap_gbrain_list_pages, "【长期记忆：清单】查看当前所有已存的记忆页面标题及其更新时间。")
