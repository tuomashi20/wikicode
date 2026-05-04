"""
constants.py - WikiCoder 指令系统的唯一真理来源。
"""

CORE_COMMANDS = {
    "/archive": "总结会话并存档到 Wiki",
    "/model": "切换 AI 模型",
    "/mode": "切换模式 (plan/build)",
    "/kbpath": "设置库路径",
    "/sync": "同步知识库索引",
    "/status": "查看运行状态",
    "/undo": "撤销上一步代码修改",
    "/pdf2md": "PDF 转 Markdown",
    "/xlsx2md": "Excel 转 Markdown",
    "/docx2md": "Word 转 Markdown",
    "/kbclear": "清除索引库",
    "/kbbackups": "查看备份列表",
    "/kbrestore": "恢复知识库备份",
    "/reset": "重置会话",
    "/resume": "恢复上次会话",
    "/help": "显示命令手册",
    "/version": "查看版本信息",
    "/exit": "退出 WikiCoder"
}

def get_command_list():
    """返回供 API 和 UI 使用的列表格式"""
    return [{"name": k, "desc": v} for k, v in CORE_COMMANDS.items()]
