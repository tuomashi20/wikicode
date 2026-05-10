import sys
import io
import traceback
from typing import Dict, Any

class PythonInterpreter:
    """[OpenCode 核心] 持久化 Python 解释器，保持变量与状态"""
    def __init__(self):
        # 共享命名空间，实现跨步状态保持
        self.globals = {
            "__name__": "__main__",
            "__doc__": None,
            "__package__": None,
            "__loader__": None,
            "__spec__": None,
            "__annotations__": {},
            "__builtins__": __builtins__
        }
        self.locals = self.globals

    def execute(self, code: str) -> str:
        """执行代码并返回标准输出与错误"""
        output_buffer = io.StringIO()
        sys.stdout = output_buffer
        sys.stderr = output_buffer
        
        try:
            # 尝试作为表达式执行
            try:
                result = eval(code, self.globals, self.locals)
                if result is not None:
                    print(result)
            except SyntaxError:
                # 如果是语句，则使用 exec
                exec(code, self.globals, self.locals)
            
            return output_buffer.getvalue() or "执行成功 (无输出)"
        except Exception:
            return traceback.format_exc()
        finally:
            sys.stdout = sys.__stdout__
            sys.stderr = sys.__stderr__

    def list_variables(self) -> Dict[str, str]:
        """列出当前内存中的关键变量（排除内置项）"""
        return {
            k: str(type(v).__name__) 
            for k, v in self.globals.items() 
            if not k.startswith("__") and k not in ("sys", "io", "traceback")
        }
