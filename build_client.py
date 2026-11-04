"""装修项目管理系统 - 单机客户端入口（PyInstaller 打包用）。

双击运行后：
1. 在 127.0.0.1:8018 启动本地服务
2. 自动用默认浏览器打开系统界面
3. 数据（data/ 数据库、uploads/ 上传件、.env 配置）保存在本程序同目录
"""
import os
import sys
import threading
import time
import webbrowser

# 保证 app 包可导入（PyInstaller 解包目录或源码目录）
_SELF = os.path.dirname(os.path.abspath(__file__))
if _SELF not in sys.path:
    sys.path.insert(0, _SELF)

import uvicorn
from app.config import BASE_DIR, DATA_DIR, ensure_data_files
from app.main import app

HOST = "127.0.0.1"
PORT = int(os.getenv("PORT", "8018"))


def _open_browser():
    try:
        time.sleep(1.8)
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    ensure_data_files()
    print("=" * 52)
    print("  装修项目管理系统（单机版）")
    print(f"  访问地址: http://{HOST}:{PORT}")
    print("  首次登录后请立即修改密码")
    print("  数据目录: " + DATA_DIR)
    print("  关闭本窗口即退出系统")
    print("=" * 52)
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT, reload=False, log_level="warning")
