"""操作审计日志工具。"""
from sqlalchemy.orm import Session

from ..models import AuditLog


def log_audit(db: Session, user, action: str, entity_type: str,
              entity_id: int | None = None, detail: str = "") -> None:
    """记录一条审计日志（独立提交，不干扰业务事务）。"""
    try:
        username = (getattr(user, "display_name", "") or getattr(user, "username", "")) if user else ""
        db.add(AuditLog(
            user_id=getattr(user, "id", None) if user else None,
            username=username or "",
            action=action or "",
            entity_type=entity_type or "",
            entity_id=entity_id,
            detail=(detail or "")[:500],
        ))
        db.commit()
    except Exception:
        db.rollback()
