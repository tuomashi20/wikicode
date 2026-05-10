import httpx
import trafilatura
from bs4 import BeautifulSoup
import urllib.parse
import logging
import re

logger = logging.getLogger("wikicoder.web")

def web_fetch(url: str) -> str:
    """访问 URL 并转换为 Markdown"""
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True, verify=False) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            
            result = trafilatura.extract(resp.text, output_format="markdown", include_links=True)
            if not result:
                soup = BeautifulSoup(resp.text, "html.parser")
                for s in soup(["script", "style", "nav", "footer"]): s.decompose()
                result = soup.get_text(separator="\n", strip=True)
                
            return result[:8000]
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return f"错误：无法访问该网页 ({e})"

def web_search(query: str) -> str:
    """使用九天 AI 搜索接口获取全网情报 (V3 规范版)"""
    from src.utils.config import load_config
    config = load_config()
    api_key = config.llm.api_key
    search_url = "https://jiutian.10086.cn/largemodel/moma/api/v3/ai_search/chat/completions"

    # 九天 AI 搜索专用格式：content 必须为对象数组
    payload = {
        "messages": [
            {
                "role": "user", 
                "content": [
                    {
                        "type": "text",
                        "text": query
                    }
                ]
            }
        ],
        "follow_ups": False,
        "stream": False
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    try:
        import httpx
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(search_url, json=payload, headers=headers)
            # 如果报错，打印原始响应以供调试
            if resp.status_code != 200:
                from src.utils.logger import get_file_logger
                get_file_logger("web_search", "web_search.log").error(f"Jiutian API Error Response: {resp.text}")
            
            resp.raise_for_status()
            data = resp.json()
            
            # 兼容性解析：九天 V3 接口在非流式下有时也会返回 delta 结构
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            delta = choice.get("delta", {})
            
            content = message.get("content") or delta.get("content") or ""
            
            return content if content else f"九天搜索未返回具体内容。原始响应结构: {list(data.keys())}"
    except Exception as e:
        from src.utils.logger import get_file_logger
        get_file_logger("web_search", "web_search.log").error(f"Jiutian Search API Exception: {str(e)}")
        return f"九天联网查询异常: {str(e)}"
