"""文件上传工具。"""
import os
import uuid
from datetime import datetime

from fastapi import UploadFile

from ..config import MAX_UPLOAD_SIZE, UPLOAD_DIR

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".txt", ".zip", ".rar", ".jpg", ".jpeg", ".png", ".gif", ".webp"}


async def save_upload(upload: UploadFile | None, subdir: str,
                      allowed: set[str] | None = None) -> str | None:
    """保存上传文件到 uploads/<subdir>/，返回可访问的 URL 路径。

    超限/类型不允许/未上传时返回 None。
    """
    if upload is None or not upload.filename:
        return None
    ext = os.path.splitext(upload.filename)[1].lower()
    if allowed is not None and ext not in allowed:
        return None

    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
    target_dir = os.path.join(UPLOAD_DIR, subdir)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, filename)

    size = 0
    with open(target_path, "wb") as f:
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_SIZE:
                f.close()
                os.remove(target_path)
                return None
            f.write(chunk)
    return f"/uploads/{subdir}/{filename}"
