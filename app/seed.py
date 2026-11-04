"""种子数据：首次启动时初始化管理员、阶段模板、演示数据。"""
from datetime import date, timedelta

from .auth import hash_password
from .database import Base, SessionLocal, engine
from .models import (
    BudgetItem, Contract, CostItem, Customer, Document, FollowUp, InventoryRecord,
    Material, Payment, Phase, Project, ProjectMaterial, ProjectPhase, PurchaseOrder,
    PurchaseOrderItem, SiteRecord, Supplier, Task, User,
)


def seed():
    """幂等初始化。已存在用户则跳过，新表按需补充演示数据。"""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).first() is None:
            _seed_initial(db)
            print("已初始化数据库与演示数据。请用初始管理员账号登录后立即修改密码。")
        _seed_extras(db)
        # 负责人改成名册下拉后，存量自由文本必须先收编进名册，
        # 否则老任务一保存就丢人。必须在 commit 之前跑。
        _seed_staff_from_existing(db)
        db.commit()
    finally:
        db.close()


def _seed_initial(db):

        # 1. 管理员
        admin = User(username="admin", password_hash=hash_password("admin123"),
                     display_name="老板", role="admin")
        db.add(admin)

        # 2. 阶段模板
        for i, name in enumerate(["拆改", "水电", "泥瓦", "木工", "油漆", "安装", "清洁验收"], 1):
            db.add(Phase(name=name, sort_order=i))

        # 3. 供应商
        s1 = Supplier(name="红星建材批发部", contact="陈老板", phone="13800001111", address="城东建材市场 A 区 12 号")
        s2 = Supplier(name="伟星水管代理商", contact="刘经理", phone="13800002222", address="城东建材市场 B 区 5 号")
        db.add_all([s1, s2])
        db.flush()

        # 4. 材料库
        materials = [
            Material(name="水泥", spec="P.O 42.5 / 50kg", brand="海螺", unit="袋", ref_price=25, supplier_id=s1.id, low_stock=20),
            Material(name="沙子", spec="中沙 / 方", brand="", unit="方", ref_price=150, supplier_id=s1.id, low_stock=5),
            Material(name="瓷砖", spec="800×800 通体大理石", brand="马可波罗", unit="片", ref_price=68, supplier_id=s1.id, low_stock=30),
            Material(name="乳胶漆", spec="18L 净味", brand="立邦", unit="桶", ref_price=380, supplier_id=s1.id, low_stock=3),
            Material(name="电线", spec="BV 2.5mm²", brand="正泰", unit="卷", ref_price=150, supplier_id=s1.id, low_stock=5),
            Material(name="PPR水管", spec="DN25 / 4米", brand="伟星", unit="根", ref_price=45, supplier_id=s2.id, low_stock=10),
            Material(name="石膏板", spec="1200×2400×9.5mm", brand="龙牌", unit="张", ref_price=32, supplier_id=s1.id, low_stock=10),
            Material(name="腻子粉", spec="20kg", brand="美巢", unit="袋", ref_price=28, supplier_id=s1.id, low_stock=15),
            Material(name="防水涂料", spec="18kg", brand="雨虹", unit="桶", ref_price=280, supplier_id=s1.id, low_stock=4),
            Material(name="木工板", spec="E0 环保 18mm", brand="兔宝宝", unit="张", ref_price=95, supplier_id=s1.id, low_stock=8),
        ]
        db.add_all(materials)
        db.flush()
        m = {m.name: m for m in materials}

        # 5. 客户
        c1 = Customer(name="张伟", phone="13900001111", community="万科城", house_type="3室2厅",
                      area=118, requirement_type="半包", budget_low=80000, budget_high=100000,
                      source="朋友介绍", status="已签约", note="在意环保材料")
        c2 = Customer(name="李婷", phone="13900002222", community="保利天悦", house_type="2室2厅",
                      area=89, requirement_type="全包", budget_low=120000, budget_high=150000,
                      source="抖音广告", status="已量房", note="准备下周出报价")
        c3 = Customer(name="王强", phone="13900003333", community="绿城桂语江南", house_type="4室2厅",
                      area=140, requirement_type="整装", budget_low=180000, budget_high=220000,
                      source="老客户转介绍", status="线索", note="")
        db.add_all([c1, c2, c3])
        db.flush()

        db.add(FollowUp(customer_id=c2.id, content="上门量房，现场沟通了厨房和卫生间改造需求，客户倾向全包。",
                        follow_at=date.today() - timedelta(days=3), next_follow_at=date.today() + timedelta(days=3)))
        db.add(FollowUp(customer_id=c3.id, content="电话首次沟通，客户预计 9 月拿房，先保持联系。",
                        follow_at=date.today() - timedelta(days=1)))

        # 6. 项目一：张伟（施工中，数据完整）
        p1 = Project(customer_id=c1.id, name="张伟·万科城3栋1单元802",
                     address="万科城 3 栋 1 单元 802", area=118, style="现代简约",
                     start_date=date.today() - timedelta(days=30), end_date=date.today() + timedelta(days=45),
                     status="施工中", progress=40, designer="小周", foreman="刘工", supervisor="老张")
        db.add(p1)
        db.flush()

        db.add(Contract(project_id=p1.id, contract_no="HT2026-001", amount=98000,
                        sign_date=date.today() - timedelta(days=40)))
        db.add_all([
            Payment(project_id=p1.id, name="签约款", plan_amount=30000, plan_date=date.today() - timedelta(days=40),
                    paid_amount=30000, paid_date=date.today() - timedelta(days=38)),
            Payment(project_id=p1.id, name="开工款", plan_amount=30000, plan_date=date.today() - timedelta(days=28),
                    paid_amount=30000, paid_date=date.today() - timedelta(days=26)),
            Payment(project_id=p1.id, name="中期款", plan_amount=28000, plan_date=date.today() + timedelta(days=20),
                    paid_amount=0),
            Payment(project_id=p1.id, name="尾款", plan_amount=10000, plan_date=date.today() + timedelta(days=50),
                    paid_amount=0),
        ])
        db.add_all([
            BudgetItem(project_id=p1.id, category="主材", item_name="瓷砖/地板/洁具", budget_amount=32000),
            BudgetItem(project_id=p1.id, category="辅材", item_name="水泥沙水电管材", budget_amount=15000),
            BudgetItem(project_id=p1.id, category="人工", item_name="各工种人工费", budget_amount=35000),
            BudgetItem(project_id=p1.id, category="设计", item_name="设计费", budget_amount=5000),
            BudgetItem(project_id=p1.id, category="管理", item_name="管理费", budget_amount=8000),
            BudgetItem(project_id=p1.id, category="其他", item_name="搬运/保洁等", budget_amount=3000),
        ])
        db.add_all([
            CostItem(project_id=p1.id, category="辅材", item_name="水泥 60 袋 + 沙子 15 方", amount=3750,
                     occurred_date=date.today() - timedelta(days=25)),
            CostItem(project_id=p1.id, category="人工", item_name="拆改 + 水电人工费", amount=9000,
                     occurred_date=date.today() - timedelta(days=22)),
            CostItem(project_id=p1.id, category="主材", item_name="伟星 PPR 水管 40 根", amount=1800,
                     occurred_date=date.today() - timedelta(days=18)),
            CostItem(project_id=p1.id, category="人工", item_name="泥瓦工预支", amount=8000,
                     occurred_date=date.today() - timedelta(days=5)),
        ])
        db.add_all([
            ProjectMaterial(project_id=p1.id, material_id=m["瓷砖"].id, plan_quantity=120, purchased_quantity=80, unit_price=68),
            ProjectMaterial(project_id=p1.id, material_id=m["乳胶漆"].id, plan_quantity=6, purchased_quantity=4, unit_price=380),
            ProjectMaterial(project_id=p1.id, material_id=m["水泥"].id, plan_quantity=60, purchased_quantity=60, unit_price=25),
            ProjectMaterial(project_id=p1.id, material_id=m["沙子"].id, plan_quantity=15, purchased_quantity=15, unit_price=150),
            ProjectMaterial(project_id=p1.id, material_id=m["PPR水管"].id, plan_quantity=40, purchased_quantity=40, unit_price=45),
        ])
        for i, name in enumerate(["拆改", "水电", "泥瓦", "木工", "油漆", "安装", "清洁验收"], 1):
            status = "已完成" if i <= 2 else ("进行中" if i == 3 else "未开始")
            db.add(ProjectPhase(project_id=p1.id, name=name, sort_order=i, status=status,
                                note="泥瓦铺贴进行中" if i == 3 else ""))
        db.add_all([
            Task(project_id=p1.id, phase_name="水电", title="水电验收", assignee="老张",
                 due_date=date.today() - timedelta(days=15), status="已完成"),
            Task(project_id=p1.id, phase_name="泥瓦", title="厨房瓷砖铺贴", assignee="刘工",
                 due_date=date.today() + timedelta(days=7), status="进行中"),
            Task(project_id=p1.id, phase_name="泥瓦", title="卫生间防水闭水试验", assignee="刘工",
                 due_date=date.today() + timedelta(days=10), status="待办"),
        ])
        db.add_all([
            SiteRecord(project_id=p1.id, record_type="施工照片", content="水电改造完成，横平竖直。",
                       record_date=date.today() - timedelta(days=14)),
            SiteRecord(project_id=p1.id, record_type="问题", content="卫生间墙面平整度偏差约 5mm，安排泥工整改。",
                       record_date=date.today() - timedelta(days=6)),
        ])
        db.add(Document(project_id=p1.id, doc_type="合同", title="装修合同 HT2026-001"))
        db.add(Document(project_id=p1.id, doc_type="图纸", title="全屋水电图 v2"))

        # 7. 项目二：李婷（待签约）
        p2 = Project(customer_id=c2.id, name="李婷·保利天悦2栋1503",
                     address="保利天悦 2 栋 1503", area=89, style="奶油风",
                     start_date=None, end_date=None, status="待签约", progress=0,
                     designer="小周", foreman="刘工", supervisor="")
        db.add(p2)
        db.flush()
        db.add(Contract(project_id=p2.id, contract_no="HT2026-002", amount=138000,
                        sign_date=None))
        db.add_all([
            Payment(project_id=p2.id, name="签约款", plan_amount=40000, plan_date=None, paid_amount=0),
            Payment(project_id=p2.id, name="开工款", plan_amount=40000, plan_date=None, paid_amount=0),
            Payment(project_id=p2.id, name="中期款", plan_amount=38000, plan_date=None, paid_amount=0),
            Payment(project_id=p2.id, name="尾款", plan_amount=20000, plan_date=None, paid_amount=0),
        ])

        # 8. 采购单与库存（项目一）
        po1 = PurchaseOrder(project_id=p1.id, supplier_id=s1.id, order_no="CG2026-001",
                            order_date=date.today() - timedelta(days=25), status="已到货",
                            note="首批辅材")
        db.add(po1)
        db.flush()
        db.add_all([
            PurchaseOrderItem(po_id=po1.id, material_id=m["水泥"].id, quantity=60, unit_price=25),
            PurchaseOrderItem(po_id=po1.id, material_id=m["沙子"].id, quantity=15, unit_price=150),
            PurchaseOrderItem(po_id=po1.id, material_id=m["PPR水管"].id, quantity=40, unit_price=45),
        ])
        db.add_all([
            InventoryRecord(material_id=m["水泥"].id, record_type="入库", quantity=100,
                            related_po_id=po1.id, note="期初备货 + 采购入库"),
            InventoryRecord(material_id=m["水泥"].id, record_type="出库", quantity=60,
                            project_id=p1.id, related_po_id=po1.id, note="项目一领用"),
            InventoryRecord(material_id=m["沙子"].id, record_type="入库", quantity=15,
                            related_po_id=po1.id, note="采购入库"),
            InventoryRecord(material_id=m["PPR水管"].id, record_type="入库", quantity=40,
                            related_po_id=po1.id, note="采购入库"),
            InventoryRecord(material_id=m["乳胶漆"].id, record_type="入库", quantity=10,
                            note="期初备货"),
        ])

        db.commit()
    # 增量演示数据（发票/供应商付款）在 seed() 中通过 _seed_extras 补充


def _seed_staff_from_existing(db):
    """把存量数据里的负责人姓名收编进人员名册。

    为什么必须做这一步：
    负责人输入框改成「只能从名册选」之后，**存量数据里的名字必须先在名册里**。
    否则老任务一打开编辑框，下拉里没有当前值（`<option>` 不存在），
    用户随手下拉一下再保存，负责人就被清空了——这不是迁移，是数据丢失。

    收编来源（三处，都是历史上可以写自由文本的地方）：
      · Task.assignee                   项目任务
      · GanttTask.assignee              工期规划任务
      · Project.designer/foreman/supervisor  项目上的三个岗位

    幂等：按姓名去重，已经在名册里的不重复插入，每次启动跑一遍都安全。
    过滤掉明显的脏值（空串、纯空格、过长的不是人名）。
    """
    from .models import GanttTask, Project, Staff, Task

    # 明显是测试/演示残留的值：收编但**停用**。
    #
    # 为什么不是直接删掉或把源字段清空：那是改用户数据。停用能让老记录的
    # 编辑框仍然带出原值（不丢人），新建时又不会出现在下拉里。用户自己
    # 到名册页面确认后可以彻底删除。
    # 「范围测试」来自早期的行级权限测试，被填进了项目 1 的监理字段。
    QUARANTINE = {"范围测试", "测试", "test", "demo", "111", "aaa"}

    existing = {s.name.strip() for s in db.query(Staff).all()}
    found: dict[str, str] = {}      # 姓名 -> 推断的工种

    def note(name, job_hint=""):
        n = (name or "").strip()
        # 人名的合理长度：1-20 字。超长的多半是备注误填进负责人了。
        if not n or len(n) > 20:
            return
        if n not in found:
            found[n] = job_hint
        elif job_hint and not found[n]:
            found[n] = job_hint

    for (a,) in db.query(Task.assignee).distinct():
        note(a)
    for (a,) in db.query(GanttTask.assignee).distinct():
        note(a)
    for d, f, s in db.query(Project.designer, Project.foreman, Project.supervisor).all():
        note(d, "设计师")
        note(f, "工长")
        note(s, "监理")

    added = 0
    for i, (name, job) in enumerate(sorted(found.items())):
        if name in existing:
            continue
        bad = name.lower() in {x.lower() for x in QUARANTINE}
        db.add(Staff(name=name, job=job, sort_order=(i + 1) * 10,
                     active=not bad,
                     note="疑似测试数据，已自动停用" if bad else ""))
        added += 1

    if added:
        print(f"已从存量数据收编 {added} 名人员到人员名册（{', '.join(sorted(found))}）。"
              f"可到「系统 → 公司人员」补充工种和电话。")


def _seed_extras(db):
    """增量补充新模块演示数据（表空时插入）。"""
    from .models import Invoice, SupplierPayment
    if db.query(Invoice).count() == 0:
        s1 = db.query(Supplier).filter(Supplier.name == "红星建材批发部").first()
        s2 = db.query(Supplier).filter(Supplier.name == "伟星水管代理商").first()
        c1 = db.query(Customer).filter(Customer.name == "张伟").first()
        p1 = db.query(Project).filter(Project.name.like("张伟%")).first()
        po1 = db.query(PurchaseOrder).filter(PurchaseOrder.order_no == "CG2026-001").first()
        db.add_all([
            Invoice(direction="进项", invoice_type="增值税专用发票", invoice_no="FP2026-001",
                    amount=3750, tax_amount=431.42,
                    invoice_date=date.today() - timedelta(days=25),
                    title="红星建材批发部", supplier_id=s1.id if s1 else None,
                    project_id=p1.id if p1 else None, po_id=po1.id if po1 else None,
                    note="水泥沙采购进项"),
            Invoice(direction="进项", invoice_type="增值税普通发票", invoice_no="FP2026-002",
                    amount=1800, tax_amount=0,
                    invoice_date=date.today() - timedelta(days=18),
                    title="伟星水管代理商", supplier_id=s2.id if s2 else None,
                    project_id=p1.id if p1 else None, po_id=po1.id if po1 else None,
                    note="PPR水管采购进项"),
            Invoice(direction="销项", invoice_type="增值税普通发票", invoice_no="FP2026-100",
                    amount=98000, tax_amount=0,
                    invoice_date=date.today() - timedelta(days=35),
                    title="张伟", customer_id=c1.id if c1 else None,
                    project_id=p1.id if p1 else None, note="张伟项目首期款开票"),
        ])
        print("已补充发票演示数据")

    if db.query(SupplierPayment).count() == 0:
        s1 = db.query(Supplier).filter(Supplier.name == "红星建材批发部").first()
        s2 = db.query(Supplier).filter(Supplier.name == "伟星水管代理商").first()
        p1 = db.query(Project).filter(Project.name.like("张伟%")).first()
        if s1 and s2:
            db.add_all([
                SupplierPayment(supplier_id=s1.id, amount=10000,
                                pay_date=date.today() - timedelta(days=20), pay_method="银行转账",
                                project_id=p1.id if p1 else None, note="首批辅材预付款"),
                SupplierPayment(supplier_id=s1.id, amount=5000,
                                pay_date=date.today() - timedelta(days=5), pay_method="微信",
                                project_id=p1.id if p1 else None, note="水泥沙尾款"),
                SupplierPayment(supplier_id=s2.id, amount=3000,
                                pay_date=date.today() - timedelta(days=12), pay_method="支付宝",
                                project_id=p1.id if p1 else None, note="水管货款"),
            ])
            print("已补充供应商付款演示数据")

    # 采购合同演示数据
    from .models import ContractMilestone, PurchaseContract
    if db.query(PurchaseContract).count() == 0:
        s1 = db.query(Supplier).filter(Supplier.name == "红星建材批发部").first()
        s2 = db.query(Supplier).filter(Supplier.name == "伟星水管代理商").first()
        db.add_all([
            PurchaseContract(contract_no="CGHT2026-001", name="红星建材辅材供货合同",
                             supplier_id=s1.id if s1 else None,
                             party_a="大有兴业（深圳）创业服务有限公司", party_b="红星建材批发部",
                             amount=5550, sign_date=date.today() - timedelta(days=30),
                             note="水泥、沙子及水电管材供货"),
            PurchaseContract(contract_no="CGHT2026-002", name="伟星水管采购合同",
                             supplier_id=s2.id if s2 else None,
                             party_a="大有兴业（深圳）创业服务有限公司", party_b="伟星水管代理商",
                             amount=1800, sign_date=date.today() - timedelta(days=20),
                             note="PPR 水管供货"),
        ])
        db.flush()
        c1 = db.query(PurchaseContract).filter(PurchaseContract.contract_no == "CGHT2026-001").first()
        c2 = db.query(PurchaseContract).filter(PurchaseContract.contract_no == "CGHT2026-002").first()
        if c1:
            db.add_all([
                ContractMilestone(contract_id=c1.id, name="签约",
                                  m_date=date.today() - timedelta(days=30)),
                ContractMilestone(contract_id=c1.id, name="首付款",
                                  m_date=date.today() - timedelta(days=25), amount=10000),
                ContractMilestone(contract_id=c1.id, name="交货",
                                  m_date=date.today() - timedelta(days=15)),
                ContractMilestone(contract_id=c1.id, name="验收",
                                  m_date=date.today() - timedelta(days=5)),
            ])
        if c2:
            db.add_all([
                ContractMilestone(contract_id=c2.id, name="签约",
                                  m_date=date.today() - timedelta(days=20)),
                ContractMilestone(contract_id=c2.id, name="交货",
                                  m_date=date.today() - timedelta(days=12)),
                ContractMilestone(contract_id=c2.id, name="验收",
                                  m_date=date.today() - timedelta(days=8)),
            ])
        print("已补充采购合同演示数据")

    # 甘特图任务演示数据
    from .models import GanttTask
    if db.query(GanttTask).count() == 0:
        p1 = db.query(Project).filter(Project.name.like("张伟%")).first()
        db.add_all([
            GanttTask(name="水电施工", start_date=date.today() - timedelta(days=35),
                      end_date=date.today() - timedelta(days=15), assignee="刘工",
                      project_id=p1.id if p1 else None, note="水电改造与验收"),
            GanttTask(name="泥瓦铺贴", start_date=date.today() - timedelta(days=15),
                      end_date=date.today() + timedelta(days=10), assignee="刘工",
                      project_id=p1.id if p1 else None, note="瓷砖铺贴与防水"),
            GanttTask(name="主材备货", start_date=date.today() + timedelta(days=2),
                      end_date=date.today() + timedelta(days=12), assignee="小周"),
        ])
        print("已补充甘特图任务演示数据")

    # 送货单演示数据（合同1已送货，合同2未送货用于展示标识）
    from .models import DeliveryNote
    if db.query(DeliveryNote).count() == 0:
        c1 = db.query(PurchaseContract).filter(PurchaseContract.contract_no == "CGHT2026-001").first()
        if c1:
            db.add(DeliveryNote(contract_id=c1.id, note_no="SHD2026-001",
                                delivery_date=date.today() - timedelta(days=15),
                                quantity=60, amount=3750, note="首批辅材送货"))
            print("已补充送货单演示数据")

    # 多岗位演示用户（仅当系统只有 admin 时补充）
    if db.query(User).count() == 1:
        db.add_all([
            User(username="gongcheng", password_hash=hash_password("123456"),
                 display_name="张工", role="工程部", is_active=True),
            User(username="caiwu", password_hash=hash_password("123456"),
                 display_name="李会计", role="财务部", is_active=True),
            User(username="caigou", password_hash=hash_password("123456"),
                 display_name="王采购", role="采购部", is_active=True),
            User(username="xingzheng", password_hash=hash_password("123456"),
                 display_name="赵行政", role="行政部", is_active=True),
        ])
        print("已补充多岗位演示用户（gongcheng/caiwu/caigou/xingzheng）。生产环境请及时修改口令或停用。")


if __name__ == "__main__":
    seed()
