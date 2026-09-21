from pathlib import Path

import uvicorn
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse

from app import main

# 创建FastAPI实例
app = FastAPI()

# 配置解决跨域问题
app.add_middleware(
    CORSMiddleware,
    # 允许Cookie跨域传递
    allow_credentials=True,
    # 任何ip都可以访问
    allow_origins=["*"],
    # 允许所有的请求方式
    allow_methods=["*"],
    # 允许所有的请求头
    allow_headers=["*"],
)

# 创建调用后端主智能体的接口
@app.get("/api/query_agent/{query}")
async def query_agent(query: str):
    # return "测试成功"
    return await main(query)


# 创建一个获取首页页面的接口
@app.get("/index")
async def get_index():
    # 获取首页的路径
    travel_agent_path = Path(__file__).parents[1]
    # 设置首页的类路径
    index_path = travel_agent_path / "pages/index.html"
    return FileResponse(
        path=index_path,
        status_code=200,
        media_type="text/html"
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")