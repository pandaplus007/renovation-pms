"""装修项目管理系统启动入口。

用法:
    python run.py            # 默认 http://0.0.0.0:8018（局域网/手机可访问）
    python run.py 9500       # 用命令行指定端口（优先级最高）

环境变量:
    HOST       监听地址，默认 0.0.0.0（局域网访问）；本地开发可设 HOST=127.0.0.1
    PORT       监听端口，默认 8018；命令行走第一参数时以命令行优先
    SECRET_KEY 会话密钥（部署时务必设置强随机值）

说明: 默认关闭热重载，保证 Windows/Linux 下运行稳定。
"""
import os
import sys

import uvicorn

if __name__ == "__main__":
    # 端口优先级：命令行参数 > 环境变量 PORT > 默认 8018
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        port = int(os.getenv("PORT", "8018"))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"\n装修项目管理系统已启动: http://{host}:{port}")
    print("首次部署请用初始管理员账号登录，并立即修改密码。\n")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
