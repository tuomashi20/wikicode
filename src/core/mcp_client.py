import asyncio
import threading
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.utils.logger import get_file_logger
from src.utils.config import load_config

logger = get_file_logger("mcp_client", "agent.log")

class GBrainMCPClient:
    """gbrain MCP 客户端桥接器"""
    
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(GBrainMCPClient, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized: return
        self._initialized = True
        self._loop = asyncio.new_event_loop()
        self._session: Optional[ClientSession] = None
        self.available_tools = []
        self._init_event = threading.Event()
        
        # 启动后台线程运行 event loop
        threading.Thread(target=self._run_event_loop, daemon=True).start()
        
        # 异步初始化
        asyncio.run_coroutine_threadsafe(self._setup_mcp(), self._loop)
        
        # 等待初始化完成信号，最多等 15 秒
        if not self._init_event.wait(timeout=15):
            logger.error("gbrain MCP initialization timed out.")

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _setup_mcp(self):
        gbrain_dir = "d:/project/wikicode/gbrain_core"
        bun_path = "C:/Users/lihq/.bun/bin/bun.exe"
        
        try:
            # 1. 准备环境变量
            env = os.environ.copy()
            dot_env = Path(gbrain_dir) / ".env"
            if dot_env.exists():
                for line in dot_env.read_text().splitlines():
                    if "=" in line and not line.startswith("#"):
                        parts = line.split("=", 1)
                        env[parts[0].strip()] = parts[1].strip()
            
            # 2. 注入 WikiCoder 的 LLM 配置 (实现配置统一)
            config = load_config()
            
            # 映射 API Key
            if config.llm.api_key:
                env["OPENAI_API_KEY"] = config.llm.api_key
                # 兼容性：某些版本的 gbrain 可能检查 JIUTIAN_API_KEY
                env["JIUTIAN_API_KEY"] = config.llm.api_key
            
            # 映射 Base URL
            # 如果 WikiCoder 没填，默认指向九天模型的标准接口
            base_url = config.llm.base_url or "https://jiutian.10086.cn/largemodel/moma/api/v3"
            env["OPENAI_BASE_URL"] = base_url
            
            # 映射模型名称
            env["OPENAI_MODEL_NAME"] = config.llm.model or "jiutian-lan-comv3"
            
            # 3. 强制对齐数据库路径 (实现项目级隔离)
            # 使用项目根目录下的 .wikicoder/gbrain 作为 gbrain 的家目录
            gbrain_home = os.path.join(os.getcwd(), ".wikicoder", "gbrain_home")
            db_path = os.path.join(gbrain_home, "brain.pglite")
            os.makedirs(gbrain_home, exist_ok=True)
            
            # 动态生成该项目的私有配置文件
            import json
            gbrain_config = {
                "engine": "pglite",
                "database_path": db_path,
                "openai_api_key": config.llm.api_key,
                "openai_base_url": base_url
            }
            with open(os.path.join(gbrain_home, "config.json"), "w") as f:
                json.dump(gbrain_config, f, indent=2)
            
            # 设置 GBRAIN_HOME，让 gbrain 彻底与系统全局配置隔离
            env["GBRAIN_HOME"] = gbrain_home
            # 确保 gbrain 进程知道它应该以这种模式运行
            env["DATABASE_URL"] = "" # 清空可能存在的全局环境变量
            env["GBRAIN_DATABASE_URL"] = ""

            server_params = StdioServerParameters(
                command=bun_path,
                args=["run", "src/cli.ts", "serve"],
                env=env,
                cwd=gbrain_dir
            )
            
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    self._session = session
                    await session.initialize()
                    
                    # 获取可用工具
                    tools_resp = await session.list_tools()
                    self.available_tools = [t.name for t in tools_resp.tools]
                    logger.info(f"Successfully connected to gbrain, found {len(self.available_tools)} tools.")
                    
                    self._init_event.set()  # 发送初始化完成信号
                    
                    # 保持连接
                    while True:
                        await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Error in MCP setup: {e}")
            self._init_event.set()  # 失败也释放锁，避免死锁

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        if not self._session:
            return "Error: gbrain MCP session not initialized."
            
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool_name, arguments), 
            self._loop
        )
        try:
            result = future.result(timeout=60)
            if result.isError:
                return f"MCP Tool Error: {result.content}"
            
            # 解析内容块
            texts = []
            for chunk in result.content:
                if hasattr(chunk, 'text'):
                    texts.append(chunk.text)
                elif isinstance(chunk, dict) and 'text' in chunk:
                    texts.append(chunk['text'])
                else:
                    texts.append(str(chunk))
            return "\n".join(texts)
        except Exception as e:
            logger.error(f"Error calling tool {tool_name}: {e}")
            return f"Error: {e}"
