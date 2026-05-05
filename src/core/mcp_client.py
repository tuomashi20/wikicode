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
            
            # 2. 注入 WikiCoder 配置 (优先级最高)
            config = load_config()
            if config.llm.api_key:
                env["OPENAI_API_KEY"] = config.llm.api_key
                env["JIUTIAN_API_KEY"] = config.llm.api_key
                if config.llm.base_url:
                    env["OPENAI_BASE_URL"] = config.llm.base_url
                    env["JIUTIAN_BASE_URL"] = config.llm.base_url

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
