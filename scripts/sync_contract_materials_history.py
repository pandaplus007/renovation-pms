"""一次性脚本：把存量合同的采购清单行（ContractMaterial）补齐到材料管理（Material）。
用法（容器内）：docker exec -w /app -i renovation-pms python - < sync_contract_materials_history.py
幂等：已有对应 Material 的行跳过；缺失的按 helper 新建/绑定，带供应商/项目/合同。
"""
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models as M

ENGINE_URL = "sqlite:////app/data/renovation.db"


def main() -> None:
    eng = create_engine(ENGINE_URL)
    db = sessionmaker(bind=eng)()
    try:
        from app.routers.contracts import _ensure_material_from_contract_line

        contracts = db.query(M.PurchaseContract).order_by(M.PurchaseContract.id).all()
        total_cm = 0
        created = 0
        updated = 0
        per_contract = []
        for ct in contracts:
            cms = db.query(M.ContractMaterial).filter(
                M.ContractMaterial.contract_id == ct.id).all()
            if not cms:
                continue
            c_created = 0
            for cm in cms:
                if not (cm.name or "").strip():
                    continue
                total_cm += 1
                exist = db.query(M.Material).filter(
                    M.Material.contract_id == ct.id,
                    M.Material.name == cm.name,
                    M.Material.spec == (cm.spec or ""),
                    M.Material.unit == (cm.unit or ""),
                ).first()
                if exist is not None:
                    continue
                _ensure_material_from_contract_line(db, cm)
                c_created += 1
            created += c_created
            per_contract.append((ct.id, ct.name, len(cms), c_created))
        db.commit()
        print("=== 补齐完成 ===")
        print("合同数:", len(contracts), "| 清单行总数:", total_cm, "| 本次补入材料:", created)
        for cid, name, n, c in per_contract[:40]:
            print(f"  合同#{cid} {name}: 清单 {n} 行, 补入 {c}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
