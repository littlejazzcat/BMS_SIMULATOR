"""BMS 模拟器入口：启动 uvicorn 服务"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run("server.app:app", host="0.0.0.0", port=8000)
