from functools import lru_cache

from langchain_mcp_adapters.client import MultiServerMCPClient


# 获取mcp客户端的函数
@lru_cache(maxsize=1)
def get_mcp_client():
    return MultiServerMCPClient(
        {
            "12306-mcp": {
                "transport": "streamable_http",
                "url": "https://mcp.api-inference.modelscope.net/10a866f960884d/mcp"
            }
        }
    )