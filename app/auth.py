"""登录、会话与密码。

安全要点
--------
- 密码哈希：优先 PBKDF2-HMAC-SHA256（标准库，260k 轮）；老数据的单轮 SHA-256
  在**登录成功时自动升级**为新格式，不需要强制全员改密码。
- 登录限流：同一账号/IP 连续失败 N 次后锁定 M 分钟，并写审计。
- 会话失效：改密码后由 User.pwd_ver 递增，登录时记入会话，校验时不符即登出。
"""
import hashlib
import hmac
import os
import secrets
import time

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from .config import SESSION_COOKIE, SESSION_MAX_AGE
from .database import get_db
from .models import User

# 供 OA 路由导入：改密码后递增以作废旧会话
SESSION_VERSION_FIELD = "pwd_ver"

_PBKDF2_ROUNDS = 260_000
_LEGACY_ROUNDS = 1

# ---- 登录限流（进程内，够单机使用；多进程部署需换 Redis） ----
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60
_login_failures: dict[str, list[float]] = {}


def _throttle_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "?"
    return f"{ip}|{username}"


def check_login_locked(request: Request, username: str) -> int:
    """返回还需等待的秒数；0 表示未锁定。"""
    rec = [t for t in _login_failures.get(_throttle_key(request, username), [])
           if time.time() - t < LOGIN_LOCK_SECONDS]
    _login_failures[_throttle_key(request, username)] = rec
    if len(rec) >= LOGIN_MAX_FAILS:
        return int(LOGIN_LOCK_SECONDS - (time.time() - rec[0]))
    return 0


def note_login_failure(request: Request, username: str) -> None:
    k = _throttle_key(request, username)
    _login_failures.setdefault(k, []).append(time.time())


def clear_login_failures(request: Request, username: str) -> None:
    _login_failures.pop(_throttle_key(request, username), None)


# ---- 密码哈希 ----

def hash_password(password: str) -> str:
    """PBKDF2-SHA256，格式 pbkdf2$<rounds>$<salt_hex>$<hash_hex>。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def _verify_legacy(password: str, stored: str) -> bool:
    """旧格式 salt$digest（单轮 SHA-256）。"""
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.sha256((salt + password).encode("utf-8")).hexdigest(), digest)


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    if stored.startswith("pbkdf2$"):
        try:
            _, rounds, salt_hex, want = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                    bytes.fromhex(salt_hex), int(rounds))
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(dk.hex(), want)
    return _verify_legacy(password, stored)


def needs_rehash(stored: str) -> bool:
    """旧单轮 SHA-256 需要升级。"""
    return bool(stored) and not stored.startswith("pbkdf2$")


# ---- 会话 ----

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """从会话读取当前登录用户；未登录返回 None。"""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    # 改过密码 → 旧会话作废
    if request.session.get("pwd_ver") != (getattr(user, SESSION_VERSION_FIELD, 1) or 1):
        request.session.clear()
        return None
    return user


def login_required(request: Request, user: User | None = Depends(get_current_user)):
    """路由依赖：未登录跳转到登录页。"""
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def establish_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["pwd_ver"] = getattr(user, SESSION_VERSION_FIELD, 1) or 1


def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
