"""全局配置。"""
import os
import sys


def _is_frozen():
    """PyInstaller 打包后运行（sys.frozen=True）。"""
    return bool(getattr(sys, "frozen", False))


if _is_frozen():
    # 打包后：exe 所在目录用于可写数据（数据库/上传/配置），随文件夹整体迁移
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    # 资源（模板/静态/默认数据文件）位于 PyInstaller 解包目录
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    RESOURCE_DIR = BASE_DIR

DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "renovation.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")


def _load_dotenv():
    """加载数据根目录 .env（KEY=VALUE），不覆盖已有环境变量。
    开发时读项目根；打包后读 exe 旁 .env，便于配置云端识别等密钥。"""
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

# 数据库连接串（个人单机使用 SQLite，后续可平滑切换 PostgreSQL）
DATABASE_URL = f"sqlite:///{DB_PATH}"

DEFAULT_SECRET = "change-me-to-a-random-secret-key"

# 会话密钥：生产环境必须用环境变量换成强随机串
SECRET_KEY = os.getenv("SECRET_KEY", DEFAULT_SECRET)
if SECRET_KEY == DEFAULT_SECRET:
    import sys as _sys
    print("[警告] SECRET_KEY 仍为默认值，会话可被伪造。"
          "请在 .env 中设置 SECRET_KEY=<32位以上随机串>", file=_sys.stderr)

SESSION_COOKIE = "renovation_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 天

# 上传限制（字节）
MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20MB


DEFAULT_DATA_FILES = ("company.json", "nav_labels.json")


def ensure_data_files():
    """确保数据目录存在；首次运行时从打包资源复制默认数据文件到 exe 旁 data/。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    for fn in DEFAULT_DATA_FILES:
        dst = os.path.join(DATA_DIR, fn)
        if os.path.exists(dst):
            continue
        src = os.path.join(RESOURCE_DIR, "data", fn)
        if os.path.exists(src):
            try:
                with open(src, "rb") as f_src, open(dst, "wb") as f_dst:
                    f_dst.write(f_src.read())
            except Exception:
                pass

# 企业微信群机器人推送（待办预警）
# 在目标群里「添加群机器人 → 企业微信群机器人」拿到 webhook 地址。
# 留空则推送功能整体空转：不发请求、不报错、不影响任何页面。
# 业主只读链接对外地址（可选）。业主用手机打开，所以要填业主能访问到的地址，
# 例如公司域名或内网穿透地址。留空则按请求头推导（本地调试够用，
# 但业主从外网访问时推导出的会是内网地址，发出去就打不开）。
PORTAL_BASE_URL = os.getenv("PORTAL_BASE_URL", "").strip().rstrip("/")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")
# 消息里的链接指向哪里。填内网可访问地址（如 http://192.168.1.100:8018），
# 手机点消息就能直接打开；留空则消息里不带链接。
WECOM_BASE_URL = os.getenv("WECOM_BASE_URL", "").strip().rstrip("/")

# Proma Cloud API Key（用于转账单图片视觉识别等 AI 能力；
# 可配置在 .env 的 PROMACLOUD_API_KEY，或部署环境变量；留空则跳过 AI 识别）
# 说明：此为旧平台配置。Proma Cloud 额度用尽后，建议改用下方 VISION_* 换第三方视觉平台，
# 保留它是为了随时切回，不影响原有部署。
PROMACLOUD_API_KEY = os.getenv("PROMACLOUD_API_KEY", "")

# 视觉识别平台（转账单图片、扫描件合同）。平台可选值与默认模型见 app/utils/vision_client.py
# - VISION_PROVIDER：dashscope（阿里云百炼）/ zhipu（智谱）/ siliconflow（硅基流动）/
#                    ark（火山方舟）/ moonshot（Kimi）/ openai / promacloud / custom
# - VISION_API_KEY ：该平台 API Key
# - VISION_BASE_URL：自定义平台地址，仅 provider=custom 时必填
# - VISION_MODEL   ：模型名，留空则用该平台默认模型
# 留空 PROVIDER 且只有 PROMACLOUD_API_KEY 时，自动沿用旧的 Proma Cloud 配置。
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "")
VISION_API_KEY = os.getenv("VISION_API_KEY", "")
VISION_BASE_URL = os.getenv("VISION_BASE_URL", "")
VISION_MODEL = os.getenv("VISION_MODEL", "")
