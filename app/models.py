"""ORM 数据模型：装修项目管理系统全部核心表。"""
import json
import re
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now():
    return datetime.now()


# ---------- 业务常量 ----------
CUSTOMER_STATUS = ["线索", "已联系", "已量房", "已报价", "已签约", "已流失"]
PROJECT_STATUS = ["待签约", "待开工", "施工中", "待验收", "已验收", "售后"]
BUDGET_CATEGORIES = ["主材", "辅材", "人工", "设计", "管理", "其他"]
COST_CATEGORIES = ["人工", "机械", "设计", "管理", "运输", "辅材", "其他费用"]
# 收款节点的三态。刻意用「收」而不是「付」：这套系统里「付」的施事方是公司
# （供应商付款页的已付 = 公司付给供应商），而收款节点的钱是**客户付给公司**。
# 早先这里写的是「未付/部分/已付」，同一个「已付」在客户门户上又表示「你已经付了」，
# 三个位置的施事方完全相反。现在收款侧一律「收」，付款侧保持「付」。
PAYMENT_STATUS = ["未收", "部分已收", "已收"]
PHASE_TEMPLATE = ["拆改", "水电", "泥瓦", "木工", "油漆", "安装", "清洁验收"]
PO_STATUS = ["待下单", "已下单", "部分到货", "已到货"]
TASK_STATUS = ["待办", "进行中", "已完成"]
RECORD_TYPES = ["施工照片", "问题", "整改", "其他"]
DOC_TYPES = ["合同", "报价单", "图纸", "效果图", "其他"]

# ---------- 审批 ----------
# 单据类型：审批覆盖的四类单据
APPROVAL_ENTITIES = [
    ("supplier_payment", "供应商付款"),
    ("purchase_contract", "采购合同"),
    ("sub_contract", "分包合同"),
    ("cost_item", "费用报销"),
]
# 审批单状态
APPROVAL_STATUS = ["待审批", "审批中", "已通过", "已驳回", "已撤回"]
# 审批动作
APPROVAL_ACTIONS = ["提交", "通过", "驳回", "撤回"]

# 启用「未审批不入账」的单据类型。
# 只有付款与费用真正影响台账与利润，合同额是签约额度而非实际支出，
# 把它纳入阻断会让记账严重滞后，故不在此列。
BLOCK_BOOKING_TYPES = {"supplier_payment", "cost_item"}
INVENTORY_TYPES = ["入库", "出库"]
ROLES = ["admin", "员工"]
INVOICE_DIRECTIONS = ["进项", "销项"]
INVOICE_TYPES = ["增值税专用发票", "增值税普通发票", "电子发票"]
INVOICE_STATUS = ["正常", "作废"]
PAY_METHODS = ["银行转账", "微信", "支付宝", "现金", "其他"]

# 作废（软删除）状态。付款与费用用这一套词，与发票的 INVOICE_STATUS 保持一致，
# 这样 state_colors.py 里已有的「作废 → idle」映射直接生效，不用再造一套配色。
# 详见 app/voided.py：为什么这两张表不做物理删除，以及口径只有一条。
VOID_STATUS = ["正常", "作废"]


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(50), default="")
    role: Mapped[str] = mapped_column(String(20), default="员工")
    is_active: Mapped[bool] = mapped_column(default=True)
    # 数据可见范围（all / assigned / own / none），见 app/row_scope.py。
    # 现有行是 NULL = 'all'，所以升级后没人会突然「看不到项目」——
    # 收紧范围必须由人在 OA 页面主动设置（加权限，而不是删权限）。
    data_scope: Mapped[str | None] = mapped_column(String(12), default="all")
    pwd_ver: Mapped[int] = mapped_column(Integer, default=1)   # 改密码后递增，作废旧会话
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Role(Base):
    """岗位（可自定义）。内置岗位 builtin=True 不允许删除，只能改权限。"""
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    builtin: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RoleModulePerm(Base):
    """岗位 → 模块 的动作位掩码（view/create/edit/delete/export/approve）。"""
    __tablename__ = "role_module_perms"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(30), index=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    flags: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("role", "module", name="uq_role_module"),)


class UserModulePerm(Base):
    """个人权限覆盖：优先于岗位。flags=0 表示显式禁止该模块。"""
    __tablename__ = "user_module_perms"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    flags: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    __table_args__ = (UniqueConstraint("user_id", "module", name="uq_user_module"),)


# ---------------------------------------------------------------- 审批流

class ApprovalFlow(Base):
    """审批流程定义：单据类型 → 串行审批岗位链。

    岗位存 role 字符串（与 User.role / RoleModulePerm.role 同一套命名）。
    steps 里排除「申请人自己的岗位」不现实——由业务侧决定是否自审；这里只保证
    链条有序，前端配置页在界面上增删节点。
    """
    __tablename__ = "approval_flows"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(60), default="")
    steps: Mapped[str] = mapped_column(Text, default="")      # JSON 数组，如 ["财务部","老板"]
    enabled: Mapped[bool] = mapped_column(default=True)
    # 未审批不入账：仅对 BLOCK_BOOKING_TYPES（付款/费用）有意义。
    # 开启后，未获审批通过的付款与费用不计入项目台账与利润报表。
    block_booking: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ApprovalRequest(Base):
    """审批单：一条业务单据对应一个审批单。

    金额与标题在提交时快照（snapshot），单据之后被修改不会篡改已发生的审批记录。
    记账不受本表影响——本表只做留痕，见模块 docstring。
    """
    __tablename__ = "approval_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="待审批", index=True)
    current_step: Mapped[int] = mapped_column(Integer, default=0)   # 当前待审节点下标，-1=尚未开始
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    submitted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("entity_type", "entity_id", name="uq_approval_entity"),)

    submitter: Mapped["User | None"] = relationship(foreign_keys=[submitted_by])
    steps: Mapped[list["ApprovalStep"]] = relationship(
        back_populates="request", cascade="all, delete-orphan", order_by="ApprovalStep.step_no")


class ApprovalStep(Base):
    """审批单的一个节点。role 为岗位名；actor 为实际处理人。"""
    __tablename__ = "approval_steps"
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("approval_requests.id"), index=True)
    step_no: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(20), default="待审批")   # 待审批/已通过/已驳回/已跳过
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(50), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    acted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    request: Mapped["ApprovalRequest"] = relationship(back_populates="steps")
    actor: Mapped["User | None"] = relationship(foreign_keys=[actor_id])


class Supplier(Base):
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(100), default="")    # 供应品类
    brand: Mapped[str] = mapped_column(String(200), default="")        # 品牌
    contact: Mapped[str] = mapped_column(String(50), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    wechat: Mapped[str] = mapped_column(String(100), default="")       # 微信号
    address: Mapped[str] = mapped_column(String(200), default="")
    profile: Mapped[str] = mapped_column(String(200), default="")      # 公司资料（证件文件等）
    tax_no: Mapped[str] = mapped_column(String(30), default="")   # 税号（开票信息）
    bank_name: Mapped[str] = mapped_column(String(100), default="")    # 默认开户银行（导入用）
    bank_account: Mapped[str] = mapped_column(String(50), default="")  # 默认银行账户（导入用）
    supply_duration: Mapped[str] = mapped_column(String(50), default="")  # 供货时长
    tax_point: Mapped[str] = mapped_column(String(50), default="")        # 合作税点
    coop_method: Mapped[str] = mapped_column(String(50), default="")      # 合作方式
    coop_level: Mapped[str] = mapped_column(String(20), default="")       # 合作等级
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    bank_accounts: Mapped[list["SupplierBankAccount"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan",
        order_by="SupplierBankAccount.id")


# ---- 公司资料知识库 ----
COMP_DOC_CATEGORIES = ["合同模板", "询价模板", "报价模板", "资质文件", "制度文档", "其他"]


class CompanyDoc(Base):
    """公司资料知识库：合同模板 / 询价模板等。"""
    __tablename__ = "company_docs"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(50), default="其他")
    note: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def filename(self) -> str:
        return self.file_path.rsplit("/", 1)[-1]


class SupplierBankAccount(Base):
    """供应商银行账户（开票信息，可多个）。"""
    __tablename__ = "supplier_bank_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    bank_name: Mapped[str] = mapped_column(String(100), default="")   # 开户行
    account_no: Mapped[str] = mapped_column(String(50), default="")   # 账号
    account_name: Mapped[str] = mapped_column(String(100), default="")  # 户名

    supplier: Mapped["Supplier"] = relationship(back_populates="bank_accounts")


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)      # 公司/客户名称
    tax_no: Mapped[str] = mapped_column(String(50), default="")    # 统一社会信用代码
    contact_person: Mapped[str] = mapped_column(String(50), default="")  # 联系人
    contact_phone: Mapped[str] = mapped_column(String(30), default="")  # 联系电话
    company_address: Mapped[str] = mapped_column(String(200), default="")  # 公司地址
    bank_name: Mapped[str] = mapped_column(String(100), default="")    # 开户银行（开票信息）
    bank_account: Mapped[str] = mapped_column(String(50), default="")  # 银行账户
    phone: Mapped[str] = mapped_column(String(30), default="")
    community: Mapped[str] = mapped_column(String(100), default="")   # 小区/楼盘（项目地点）
    house_type: Mapped[str] = mapped_column(String(50), default="")   # 户型
    area: Mapped[float] = mapped_column(Float, default=0)             # 面积(㎡)
    requirement_type: Mapped[str] = mapped_column(String(20), default="")  # 整装/半包/全包
    budget_low: Mapped[float] = mapped_column(Float, default=0)
    budget_high: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(50), default="")       # 来源渠道
    status: Mapped[str] = mapped_column(String(20), default=CUSTOMER_STATUS[0], index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    follow_ups: Mapped[list["FollowUp"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", order_by="FollowUp.follow_at.desc()")
    projects: Mapped[list["Project"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan")
    quotations: Mapped[list["Quotation"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan")


class FollowUp(Base):
    __tablename__ = "follow_ups"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    follow_at: Mapped[datetime] = mapped_column(DateTime, default=now)      # 本次跟进时间
    next_follow_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 下次跟进
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    customer: Mapped["Customer"] = relationship(back_populates="follow_ups")


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    address: Mapped[str] = mapped_column(String(200), default="")
    area: Mapped[float] = mapped_column(Float, default=0)
    style: Mapped[str] = mapped_column(String(50), default="")          # 装修风格
    start_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PROJECT_STATUS[0], index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)           # 0-100
    designer: Mapped[str] = mapped_column(String(50), default="")
    foreman: Mapped[str] = mapped_column(String(50), default="")
    supervisor: Mapped[str] = mapped_column(String(50), default="")
    # 创建者（users 外键）。行级权限里「我创建的项目」靠这一列精确匹配，
    # 不靠人名文本——文本会重名、也会随改名失效。
    # 存量项目的这一列是 NULL，属正常：无从推断是谁建的，由管理员认领。
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    customer: Mapped["Customer"] = relationship(back_populates="projects")
    contract: Mapped["Contract | None"] = relationship(
        back_populates="project", cascade="all, delete-orphan", uselist=False)
    payments: Mapped[list["Payment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="Payment.sort_order, Payment.id")
    budget_items: Mapped[list["BudgetItem"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    cost_items: Mapped[list["CostItem"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="CostItem.occurred_date.desc()")
    project_materials: Mapped[list["ProjectMaterial"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    phases: Mapped[list["ProjectPhase"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ProjectPhase.sort_order")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Task.due_date")
    site_records: Mapped[list["SiteRecord"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="SiteRecord.record_date.desc()")
    documents: Mapped[list["Document"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    notes: Mapped[list["ProjectNote"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="ProjectNote.note_date.desc(), ProjectNote.id.desc()")
    change_orders: Mapped[list["ChangeOrder"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="ChangeOrder.raise_date.desc(), ChangeOrder.id.desc()")

    # ---------- 两个「进度」必须分开 ----------
    #
    # progress 字段的含义是**施工进度**（阶段完成比例，见 routers/projects.py
    # 的 recalc_progress）；没有阶段数据时保持原值。两个项目都没建阶段，
    # 所以它一直是 0 —— 数字没错，只是没有数据源。
    #
    # recv_pct 是**回款比例**（已收 ÷ 合同额），由收款记录实时算出，
    # 永远有值。这两个数在装修行业经常反向：预付款多的项目回款 90%
    # 而施工才 30%；也有做完了才收尾款的，回款 10% 而施工 100%。
    # 把收款比例写进 progress 会让人读成「工程完成了多少」，
    # 所以这里只读、不落库，两个数在页面上各自标明。
    @property
    def recv_pct(self):
        """回款比例（%）。无合同时返回 None 而不是 0。

        返回 None 是刻意的：0% 会被读成「一分钱都没收到」，
        而没有合同的项目是「还没到收钱的时候」，两回事。模板据此显示「—」。

        超收时故意不截断到 100：收到 112% 本身就是要看的信息，
        截掉就成了「刚好收满」。进度条宽度另由模板 clamp。
        """
        amount = self.contract.amount if self.contract else 0
        if not amount:
            return None
        return self.recv_amount / amount * 100

    @property
    def recv_amount(self):
        """已收合计。与 recv_pct 同源，避免两处各写一遍求和。

        排除已作废的收款节点：作废 = 这笔钱不算数。漏这一处会让
        「阶段未开始 / 回款 90%」这类判断直接读错——而这两个数在装修
        项目里经常反向，老板问起来时正要拿它判断现金流。
        """
        return sum(p.paid_amount for p in self.payments
                   if getattr(p, "void_status", "正常") != "作废")

    @property
    def phase_never_started(self):
        """是否「声称在施工，却一个阶段都没开始」——两个数据源打架。

        为什么不是「阶段未更新」：project_phases 表**没有任何时间字段**
        （没有 created_at / updated_at / start_date），所以「多久没更新」
        在数据上无法判定，写出来只能靠新增字段去猜。与其给一个编造的
        时长，不如指出一件**确实可验证**的事。

        这件事是：status 说项目在施工中，阶段表却一步没动。两个数据源
        必有一个是错的——要么阶段没打勾（那 0% 没有信息量），要么 status
        填错了（那项目实际还没开工）。不论哪种，都需要人去看一眼。

        刻意不判的情况：
          · status 不在施工态（待签约/待开工本就该没阶段开始，不是矛盾）
          · 没有阶段记录（那是「没录阶段」，不是「录了但没开始」）
        """
        if self.status not in ("施工中", "待验收"):
            return False
        if not self.phases:
            return False
        return all(ph.status == "未开始" for ph in self.phases)


class Staff(Base):
    """公司人员名册。

    为什么单独建表，而不是直接放开用 users：
    现场真正干活的人——工长、泥瓦师傅、水电师傅、设计师——绝大多数**不登录系统**。
    改版前这些人只能以自由文本存在（`Task.assignee` / `GanttTask.assignee` /
    `Project.designer|foreman|supervisor`），于是同一个人的写法会分裂成
    「刘工」「刘师傅」「老刘」三种，按人筛选、按人统计全部失效。
    实测生产库里任务写的是「刘工」「小周」，而 users 表只有 admin/boss 两个
    老板账号——把负责人下拉挂到 users 上，下拉里一个人都选不出来。

    所以这里是「人」的名册，不是「账号」的名册：
      · 进名册**不需要**密码、权限、登录能力；
      · 名册里的人可以被指派任务，但不一定能登录系统；
      · 需要登录的人另外在 OA 里开账号，与本表互不依赖。

    与 users 的关系：可选地绑一个账号（`user_id`）。绑了才能在「我的任务」
    这类按账号精确筛选的地方命中；不绑就只是一份名单里的名字。
    """
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), index=True)
    # 工种：工长 / 设计师 / 监理 / 泥瓦 / 水电 / 木工 / 油漆 / 安装 / 其他。
    # 刻意存字符串而不是外键——工种是给人看的分类，不是权限维度，
    # 加一个工种不该需要改表。
    job: Mapped[str] = mapped_column(String(30), default="")
    phone: Mapped[str] = mapped_column(String(30), default="")
    # 在职状态。离职的人**不删**：历史任务里还写着他的名字，
    # 删掉之后那些任务的负责人就变成悬空的字符串。停用只是不再出现在
    # 新建任务的下拉里（编辑老任务时仍会保留当前值，否则一保存就丢人）。
    active: Mapped[bool] = mapped_column(default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    # 可选绑定登录账号。NULL = 纯名册人员，不参与按账号的筛选。
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True)
    user: Mapped["User | None"] = relationship()


class Contract(Base):
    """施工合同（与客户/业主签订）。"""
    __tablename__ = "contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), unique=True)
    contract_no: Mapped[str] = mapped_column(String(50), default="")
    name: Mapped[str] = mapped_column(String(200), default="")        # 合同名称
    party_a: Mapped[str] = mapped_column(String(200), default="")     # 甲方（客户/业主）
    party_b: Mapped[str] = mapped_column(String(200), default="")     # 乙方（本公司）
    amount: Mapped[float] = mapped_column(Float, default=0)            # 合同总金额
    settle_amount: Mapped[float] = mapped_column(Float, default=0)      # 结算金额
    sign_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    start_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)  # 开工日期
    end_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)    # 竣工日期
    duration_days: Mapped[int] = mapped_column(Integer, default=0)            # 工期天数
    a_contact: Mapped[str] = mapped_column(String(100), default="")          # 甲方联系人/电话
    b_contact: Mapped[str] = mapped_column(String(100), default="")          # 乙方联系人/电话
    pay_method: Mapped[str] = mapped_column(String(50), default="")          # 付款方式
    tax_rate: Mapped[float] = mapped_column(Float, default=0)                 # 税率%
    prepay_rate: Mapped[float] = mapped_column(Float, default=0)              # 预付款比例%
    warranty_months: Mapped[int] = mapped_column(Integer, default=0)          # 质保期月
    warranty_retention_rate: Mapped[float] = mapped_column(Float, default=0)  # 质保金比例%
    pay_terms: Mapped[str] = mapped_column(Text, default="")             # 付款节点表（JSON）
    file_path: Mapped[str] = mapped_column(String(500), default="")   # 合同文件
    note: Mapped[str] = mapped_column(Text, default="")

    project: Mapped["Project"] = relationship(back_populates="contract")


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))                       # 节点：签约款/开工款/中期款/尾款
    plan_amount: Mapped[float] = mapped_column(Float, default=0)        # 计划金额
    plan_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    paid_amount: Mapped[float] = mapped_column(Float, default=0)        # 已收金额
    paid_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)          # 节点手动排序
    # 作废制（软删除）：收款节点不物理删除。
    # 这张表尤其不能删：receipts 是 cascade="all, delete-orphan"，
    # 删节点会连带删掉它下面所有收款凭证（含已上传的转账单），一次误操作
    # 就让法律凭据永久消失。作废后行保留、可审计、可恢复，不进已收口径。
    #
    # ⚠ 列名刻意叫 `void_status` 而不是 `status`：
    #   本类已经有一个 `status` @property（未收/部分/已收清，由已收金额
    #   算出来的**业务状态**），模板里 `p.status | state_badge` 用的就是它。
    #   同名会与该 property 撞车——SQLAlchemy 把 `model.status` 解析成
    #   property 对象，于是 `not_voided(Payment)` 报
    #   「'property' object has no attribute 'is_'」，且**只在收款相关
    #   页面触发**，报错点离根因很远。
    #   两个「状态」语义也本就不同：一个说「这笔收得怎么样」，一个说
    #   「这张单还算不算数」。
    void_status: Mapped[str] = mapped_column("status", String(10), default="正常", index=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="")
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="payments")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="payment")
    receipts: Mapped[list["PaymentReceipt"]] = relationship(
        back_populates="payment", cascade="all, delete-orphan",
        order_by="PaymentReceipt.receipt_date")

    @property
    def status(self) -> str:
        if self.paid_amount <= 0:
            return PAYMENT_STATUS[0]
        if self.paid_amount >= self.plan_amount:
            return PAYMENT_STATUS[2]
        return PAYMENT_STATUS[1]

    @property
    def balance(self) -> float:
        return self.plan_amount - self.paid_amount


class PaymentReceipt(Base):
    """收款节点下的收款明细（客户分笔付款，可附甲方转账单凭证）。"""
    __tablename__ = "payment_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    receipt_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    payer: Mapped[str] = mapped_column(String(100), default="")        # 付款人
    payee: Mapped[str] = mapped_column(String(100), default="")        # 收款人
    payer_account: Mapped[str] = mapped_column(String(50), default="")  # 付款账号
    payee_account: Mapped[str] = mapped_column(String(50), default="")  # 收款账号
    payer_bank: Mapped[str] = mapped_column(String(100), default="")    # 付款开户行
    payee_bank: Mapped[str] = mapped_column(String(100), default="")    # 收款开户行
    purpose: Mapped[str] = mapped_column(String(200), default="")       # 用途/摘要
    note: Mapped[str] = mapped_column(Text, default="")       # 付款方式/备注
    file_path: Mapped[str] = mapped_column(String(500), default="")  # 甲方转账单凭证
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 作废制：客户回款凭证。file_path 是法律凭据，绝不能靠删记录来「撤销」——
    # 作废后行与附件都保留，不计入已收。
    status: Mapped[str] = mapped_column(String(10), default="正常", index=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="")
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    payment: Mapped["Payment"] = relationship(back_populates="receipts")


class BudgetItem(Base):
    __tablename__ = "budget_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default="其他")
    item_name: Mapped[str] = mapped_column(String(100), default="")
    budget_amount: Mapped[float] = mapped_column(Float, default=0)

    project: Mapped["Project"] = relationship(back_populates="budget_items")


class CostItem(Base):
    """项目费用（人工 / 机械 / 设计 / 管理 / 运输等，不含材料采购）。"""
    __tablename__ = "cost_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default="其他费用")
    item_name: Mapped[str] = mapped_column(String(100), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)
    occurred_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")   # 费用凭证附件
    # 作废制：录错不物理删除，改标记作废（行保留，但不进任何金额口径）
    status: Mapped[str] = mapped_column(String(10), default="正常", index=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="")  # 作废原因
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="cost_items")


class Material(Base):
    __tablename__ = "materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    spec: Mapped[str] = mapped_column(String(100), default="")          # 规格
    model_no: Mapped[str] = mapped_column(String(100), default="")      # 型号
    brand: Mapped[str] = mapped_column(String(50), default="")
    unit: Mapped[str] = mapped_column(String(20), default="件")         # 单位：袋/桶/㎡/米...
    ref_price: Mapped[float] = mapped_column(Float, default=0)          # 参考单价（含税）
    net_price: Mapped[float] = mapped_column(Float, default=0)          # 不含税单价
    tax_rate: Mapped[float] = mapped_column(Float, default=0)           # 税率 %（如 13）
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)  # 所属项目
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_contracts.id"), nullable=True)  # 所属采购合同
    low_stock: Mapped[float] = mapped_column(Float, default=0)          # 低库存预警阈值
    opening_stock: Mapped[float] = mapped_column(Float, default=0)      # 期初库存
    stock: Mapped[float] = mapped_column(Float, default=0)              # 当前库存（期初 + 入库 - 出库）
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    supplier: Mapped["Supplier | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    contract: Mapped["PurchaseContract | None"] = relationship()
    inventory_records: Mapped[list["InventoryRecord"]] = relationship(
        back_populates="material", cascade="all, delete-orphan")

    @property
    def low_stock_flag(self) -> bool:
        return self.low_stock > 0 and (self.stock or 0) <= self.low_stock


class ProjectMaterial(Base):
    __tablename__ = "project_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    plan_quantity: Mapped[float] = mapped_column(Float, default=0)      # 预算用量
    purchased_quantity: Mapped[float] = mapped_column(Float, default=0)  # 已采购量
    unit_price: Mapped[float] = mapped_column(Float, default=0)         # 成交单价
    note: Mapped[str] = mapped_column(Text, default="")

    project: Mapped["Project"] = relationship(back_populates="project_materials")
    material: Mapped["Material"] = relationship()

    @property
    def gap(self) -> float:
        """缺口 = 预算用量 - 已采购量。"""
        return round(self.plan_quantity - self.purchased_quantity, 2)

    @property
    def amount(self) -> float:
        return round(self.purchased_quantity * self.unit_price, 2)


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_contracts.id"), nullable=True)
    subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("sub_contracts.id"), nullable=True)  # 所属采购合同
    order_no: Mapped[str] = mapped_column(String(50), default="")
    order_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=PO_STATUS[0], index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project: Mapped["Project | None"] = relationship()
    supplier: Mapped["Supplier | None"] = relationship()
    contract: Mapped["PurchaseContract | None"] = relationship()
    items: Mapped[list["PurchaseOrderItem"]] = relationship(
        back_populates="po", cascade="all, delete-orphan")

    @property
    def total_amount(self) -> float:
        """采购单合计：按明细原始单价×数量求和后统一四舍五入，避免逐行舍入累计误差。"""
        return round(sum((i.quantity or 0) * (i.unit_price or 0) for i in self.items), 2)


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    quantity: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="")   # 下单时单位快照

    po: Mapped["PurchaseOrder"] = relationship(back_populates="items")
    material: Mapped["Material"] = relationship()

    @property
    def amount(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class InventoryRecord(Base):
    __tablename__ = "inventory_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(10), default="入库")
    quantity: Mapped[float] = mapped_column(Float, default=0)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    related_po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    material: Mapped["Material"] = relationship(back_populates="inventory_records")
    project: Mapped["Project | None"] = relationship()


class Phase(Base):
    """阶段模板（全局一份，新项目创建时自动复制为 ProjectPhase）。"""
    __tablename__ = "phases"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ProjectPhase(Base):
    __tablename__ = "project_phases"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="未开始")   # 未开始/进行中/已完成
    note: Mapped[str] = mapped_column(Text, default="")

    project: Mapped["Project"] = relationship(back_populates="phases")


class Task(Base):
    """项目任务。

    `assignee` 刻意保持**自由文本**而不是 users 外键：现场写「张工」「小王」
    比从下拉里选人顺手，而姓名一旦不是外键就没法做「我的任务」筛选——
    这正是加 `assignee_user_id` 的原因，两者并存：
      - `assignee`     —— 兼容历史数据与随手输入，始终有值
      - `assignee_user_id` —— 指向账号，用于「我负责的」这类**精确筛选**
    新增任务时两边一起写；改了名字只改 assignee，不动外键。
    """
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    phase_name: Mapped[str] = mapped_column(String(50), default="")     # 所属阶段（冗余存名称，便于展示）
    title: Mapped[str] = mapped_column(String(200))
    assignee: Mapped[str] = mapped_column(String(50), default="")
    assignee_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True, index=True)   # 「我的任务」用
    due_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=TASK_STATUS[0])
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 子任务：指向同表的父任务。只支持一层（父任务不能再有 parent），
    # NULL = 顶层任务，历史数据因此不需要回填。
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True, index=True)

    project: Mapped["Project"] = relationship(back_populates="tasks")
    assignee_user: Mapped["User | None"] = relationship()
    parent: Mapped["Task | None"] = relationship(
        remote_side="Task.id", back_populates="subtasks")
    # 删父任务时连同子任务一起删（cascade），否则会留下 parent_id 指向
    # 已删除行的孤儿任务，在任务总览里显示成没有归属的条目。
    subtasks: Mapped[list["Task"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", order_by="Task.id")

    @property
    def is_overdue(self) -> bool:
        """是否逾期未完成。

        判据只看「有截止日 + 已过期 + 没完成」，**刻意不加宽限期**：
        装修场景里「昨天该交」的活今天就是逾期，加缓冲反而让人以为还没到期。
        没有截止日期的不算逾期——那不是「忘了做」，是「还没定日期」，
        混进逾期清单只会稀释真正紧急的那几条。
        """
        if self.status in ("已完成", "已取消"):
            return False
        if not self.due_date:
            return False
        d = self.due_date.date() if isinstance(self.due_date, datetime) else self.due_date
        return d < date.today()


class ProjectPortalLink(Base):
    """业主只读链接（一个项目一条，可停用）。

    这是把数据放到业主（甚至公网）上的入口，所以设计上刻意收紧：
      - `token` 用 secrets 生成（192 位熵），不是自增 id
      - `enabled=False` 即刻吊销，token 还在库里也拿不到数据
      - `expires_at` 可设过期，避免「链接发出去了就永远有效」
      - `show_photos` 默认 False：现场照片常拍到业主私人物品与邻居门牌
      - `view_count` / `last_viewed_at` 记录访问——除了审计，
        还支撑「业主三天没看了，该催一下」的判断
    可见字段的挑选在 app/portal.py 的 build_view()，那是唯一出口。
    """
    __tablename__ = "project_portal_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id"), unique=True, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(default=False, index=True)
    show_photos: Mapped[bool] = mapped_column(default=False)
    show_payment: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    last_viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project: Mapped["Project"] = relationship()


class SiteRecord(Base):
    __tablename__ = "site_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(20), default=RECORD_TYPES[0])
    content: Mapped[str] = mapped_column(Text, default="")
    record_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    image_path: Mapped[str] = mapped_column(String(500), default="")   # 现场照片 URL 路径
    note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="")        # 问题状态：待处理/处理中/已解决

    project: Mapped["Project"] = relationship(back_populates="site_records")
    followups: Mapped[list["SiteRecordFollowup"]] = relationship(
        back_populates="record", cascade="all, delete-orphan",
        order_by="SiteRecordFollowup.id")


class SiteRecordFollowup(Base):
    """现场记录后续跟进（问题处理过程）。"""
    __tablename__ = "site_record_followups"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_record_id: Mapped[int] = mapped_column(ForeignKey("site_records.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    record: Mapped["SiteRecord"] = relationship(back_populates="followups")


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(20), default="其他")
    title: Mapped[str] = mapped_column(String(200), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    project: Mapped["Project"] = relationship(back_populates="documents")


class Invoice(Base):
    """发票：进项（收到供应商发票）/ 销项（开给客户）。"""
    __tablename__ = "invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    direction: Mapped[str] = mapped_column(String(10), default="进项", index=True)
    invoice_type: Mapped[str] = mapped_column(String(30), default="增值税普通发票")
    invoice_no: Mapped[str] = mapped_column(String(50), default="")
    amount: Mapped[float] = mapped_column(Float, default=0)          # 价税合计金额
    tax_excl: Mapped[float] = mapped_column(Float, default=0)        # 不含税价
    tax_amount: Mapped[float] = mapped_column(Float, default=0)      # 税额（可选）
    tax_rate: Mapped[float] = mapped_column(Float, default=0)        # 税率%（如 13），可选
    refund_status: Mapped[str] = mapped_column(String(30), default="")  # 退税状态：可退/已结转/不可退/缺合同等
    invoice_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    title: Mapped[str] = mapped_column(String(200), default="")     # 购方名称（抬头）
    tax_no: Mapped[str] = mapped_column(String(50), default="")     # 购方税号
    seller_name: Mapped[str] = mapped_column(String(200), default="")   # 销方名称
    seller_tax_no: Mapped[str] = mapped_column(String(50), default="")  # 销方税号
    file_path: Mapped[str] = mapped_column(String(500), default="")  # PDF 发票附件路径
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True)
    subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("sub_contracts.id"), nullable=True)
    suborder_id: Mapped[int | None] = mapped_column(ForeignKey("sub_orders.id"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)  # 关联收款节点
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_contracts.id"), nullable=True)  # 关联采购合同
    status: Mapped[str] = mapped_column(String(10), default="正常")  # 正常/作废
    note: Mapped[str] = mapped_column(Text, default="")
    # 由哪张开票申请单开出（销项专用；进项发票恒为 NULL）。
    # 为什么要这一列：一张申请单可以分次开出多张发票，靠 invoice_id 单字段
    # 反查只能记住最后一张，其余会被当成「历史直录发票」再算一遍，
    # 导致「已开票」统计翻倍。详见 sales_invoices.received_vs_invoiced。
    app_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoice_applications.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    supplier: Mapped["Supplier | None"] = relationship()
    customer: Mapped["Customer | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    po: Mapped["PurchaseOrder | None"] = relationship()
    payment: Mapped["Payment | None"] = relationship()
    contract: Mapped["PurchaseContract | None"] = relationship()
    # 反向关系不用声明：invoices.app_id → invoice_applications.id 与
    # invoice_applications.invoice_id → invoices.id 构成两条外键路径，
    # 写任一方的 relationship 都必须显式指定 foreign_keys 才不会让
    # SQLAlchemy 在启动时报 "could not determine join condition"。
    # 现有代码都从 InvoiceApplication 侧查发票，不需要反向关系。


class SupplierPayment(Base):
    """供应商付款记录。"""
    __tablename__ = "supplier_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"), index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    pay_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    pay_method: Mapped[str] = mapped_column(String(20), default="银行转账")
    bank_file: Mapped[str] = mapped_column(String(500), default="")   # 银行转账单附件路径
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_contracts.id"), nullable=True)  # 关联采购合同
    subcontract_id: Mapped[int | None] = mapped_column(ForeignKey("sub_contracts.id"), nullable=True)  # 关联分包合同
    suborder_id: Mapped[int | None] = mapped_column(ForeignKey("sub_orders.id"), nullable=True)  # 关联分包采购单
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 作废制：录错不物理删除，改标记作废（行保留，但不进任何金额口径）
    status: Mapped[str] = mapped_column(String(10), default="正常", index=True)
    void_reason: Mapped[str] = mapped_column(String(200), default="")  # 作废原因
    voided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    supplier: Mapped["Supplier"] = relationship()
    project: Mapped["Project | None"] = relationship()
    po: Mapped["PurchaseOrder | None"] = relationship()
    contract: Mapped["PurchaseContract | None"] = relationship()


class SubContract(Base):
    """分包合同：总包把工程分包给专业分包商。"""
    __tablename__ = "sub_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(60), default="")
    name: Mapped[str] = mapped_column(String(200), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    party_a: Mapped[str] = mapped_column(String(200), default="")       # 甲方（本公司）
    party_b: Mapped[str] = mapped_column(String(200), default="")       # 分包商
    a_tax_no: Mapped[str] = mapped_column(String(50), default="")       # 甲方税号
    a_address: Mapped[str] = mapped_column(String(200), default="")     # 甲方地址
    a_contact: Mapped[str] = mapped_column(String(50), default="")      # 甲方联系人
    a_bank: Mapped[str] = mapped_column(String(100), default="")        # 甲方开户行
    a_account: Mapped[str] = mapped_column(String(50), default="")      # 甲方账号
    b_tax_no: Mapped[str] = mapped_column(String(50), default="")       # 乙方税号
    b_bank: Mapped[str] = mapped_column(String(100), default="")        # 乙方开户行
    b_account: Mapped[str] = mapped_column(String(50), default="")      # 乙方账号
    duration_days: Mapped[int] = mapped_column(Integer, default=0)       # 工期（天）
    file_path: Mapped[str] = mapped_column(String(500), default="")     # 合同文件路径
    amount: Mapped[float] = mapped_column(Float, default=0)             # 分包合同额
    category: Mapped[str] = mapped_column(String(50), default="劳务分包")  # 费用分类：劳务分包/专业分包/机械租赁/其他
    sign_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    orders: Mapped[list["SubOrder"]] = relationship(
        back_populates="subcontract", cascade="all, delete-orphan", order_by="SubOrder.sort_order, SubOrder.id")
    materials: Mapped[list["SubContractMaterial"]] = relationship(
        back_populates="subcontract", cascade="all, delete-orphan", order_by="SubContractMaterial.id")
    milestones: Mapped[list["SubContractMilestone"]] = relationship(
        back_populates="subcontract", cascade="all, delete-orphan", order_by="SubContractMilestone.id")


class SubOrder(Base):
    """分包采购单：分包合同下的下单记录。"""
    __tablename__ = "sub_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    subcontract_id: Mapped[int] = mapped_column(ForeignKey("sub_contracts.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(60), default="")
    total_amount: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="已下单")
    order_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)          # 分包采购单手动排序
    subcontract: Mapped["SubContract"] = relationship(back_populates="orders")


class SubContractMaterial(Base):
    """分包合同采购清单（材料/项目明细）。"""
    __tablename__ = "sub_contract_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    subcontract_id: Mapped[int] = mapped_column(ForeignKey("sub_contracts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    spec: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="")
    quantity: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    subcontract: Mapped["SubContract"] = relationship(back_populates="materials")


class SubContractMilestone(Base):
    """分包合同时间节点。"""
    __tablename__ = "sub_contract_milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    subcontract_id: Mapped[int] = mapped_column(ForeignKey("sub_contracts.id"), index=True)
    name: Mapped[str] = mapped_column(String(60), default="")
    m_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    subcontract: Mapped["SubContract"] = relationship(back_populates="milestones")


class PurchaseContract(Base):
    """采购合同。"""
    __tablename__ = "purchase_contracts"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_no: Mapped[str] = mapped_column(String(60), default="")
    name: Mapped[str] = mapped_column(String(200), index=True)
    category: Mapped[str] = mapped_column(String(50), default="")    # 分类（可自定义）
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)  # 所属项目
    party_a: Mapped[str] = mapped_column(String(200), default="")    # 甲方
    party_b: Mapped[str] = mapped_column(String(200), default="")    # 乙方
    a_tax_no: Mapped[str] = mapped_column(String(50), default="")    # 甲方税号
    a_address: Mapped[str] = mapped_column(String(200), default="")  # 甲方地址
    a_contact: Mapped[str] = mapped_column(String(50), default="")   # 甲方联系人
    a_bank: Mapped[str] = mapped_column(String(100), default="")     # 甲方开户行
    a_account: Mapped[str] = mapped_column(String(50), default="")   # 甲方账号
    b_tax_no: Mapped[str] = mapped_column(String(50), default="")    # 乙方税号
    b_bank: Mapped[str] = mapped_column(String(100), default="")     # 乙方开户行
    b_account: Mapped[str] = mapped_column(String(50), default="")   # 乙方账号
    amount: Mapped[float] = mapped_column(Float, default=0)
    sign_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    file_path: Mapped[str] = mapped_column(String(500), default="")   # 合同文件路径
    note: Mapped[str] = mapped_column(Text, default="")
    no_delivery: Mapped[bool] = mapped_column(Boolean, default=False)  # 无需送货单（如服务类合同）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    supplier: Mapped["Supplier | None"] = relationship()
    project: Mapped["Project | None"] = relationship()
    milestones: Mapped[list["ContractMilestone"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan",
        order_by="ContractMilestone.m_date")
    materials: Mapped[list["ContractMaterial"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan")
    delivery_notes: Mapped[list["DeliveryNote"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan",
        order_by="DeliveryNote.delivery_date.desc()")


class ContractMilestone(Base):
    """采购合同时间节点（付款/交货/验收等）。"""
    __tablename__ = "contract_milestones"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    m_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0)          # 节点金额（付款节点）
    note: Mapped[str] = mapped_column(Text, default="")

    contract: Mapped["PurchaseContract"] = relationship(back_populates="milestones")


class ProjectNote(Base):
    """施工笔记：记录施工过程事件备忘。"""
    __tablename__ = "project_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    note_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)  # 记录日期
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project: Mapped["Project"] = relationship(back_populates="notes")


class GanttTask(Base):
    """甘特图通用任务（可关联项目，有起止日期，以时间条展示）。

    看板拖拽（2026-10）新增两个字段：
      status     看板列位。**刻意复用 ProjectPhase 的三档词**（未开始/进行中/
                 已完成），这样甘特图与施工阶段说的是同一套话，`state_badge`
                 直接命中 STATE_TONE_MAP，不需要为看板单开一套映射。
      sort_order 列内排序。拖拽后由前端提交整列顺序，服务端只按提交值重排。
                 默认 0 —— 历史数据全为 0，看板里按 id 兜底稳定排序。
    """
    __tablename__ = "gantt_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    start_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    assignee: Mapped[str] = mapped_column(String(50), default="")
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(20), default="未开始")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    project: Mapped["Project | None"] = relationship()

    # 挂在哪个施工阶段下（可空）。
    #
    # 加这一列是为了收掉「两套进度」：改版前 gantt_tasks 和 project_phases
    # 各说各的，页面上并存却互不相通——实测 gantt_tasks 只有 2 条且
    # project_id 全为 NULL（种子数据），而 project_phases 有 17 条全部
    # 挂在项目下。用户不知道该往哪填，于是两套都没人填。
    #
    # 现在的关系是「阶段是骨架，任务挂在阶段下」：一个阶段可以有多个任务，
    # 任务的日期决定阶段在甘特图上的条。phase_id 为空 = 未排期的零散任务，
    # 甘特图单独列一节，不丢。
    #
    # 只加列不做数据迁移：存量 2 条任务的 phase_id 为 NULL，
    # 落在「未排期」那一节里，人工认领即可——猜它属于哪个阶段只会猜错。
    phase_id: Mapped[int | None] = mapped_column(
        ForeignKey("project_phases.id"), nullable=True, index=True)
    phase: Mapped["ProjectPhase | None"] = relationship()


class ContractMaterial(Base):
    """采购合同清单材料。"""
    __tablename__ = "contract_materials"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    spec: Mapped[str] = mapped_column(String(200), default="")
    brand: Mapped[str] = mapped_column(String(100), default="")
    unit: Mapped[str] = mapped_column(String(20), default="件")
    quantity: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)  # 材料供应商
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)    # 所属项目
    note: Mapped[str] = mapped_column(Text, default="")

    contract: Mapped["PurchaseContract"] = relationship(back_populates="materials")
    supplier: Mapped["Supplier | None"] = relationship()
    project: Mapped["Project | None"] = relationship()


class DeliveryNote(Base):
    """采购合同送货单。"""
    __tablename__ = "delivery_notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("purchase_contracts.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    po_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id"), nullable=True)
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_payments.id"), nullable=True)
    note_no: Mapped[str] = mapped_column(String(60), default="")
    delivery_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    quantity: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)
    file_path: Mapped[str] = mapped_column(String(500), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    contract: Mapped["PurchaseContract"] = relationship(back_populates="delivery_notes")


class ProjectLedger(Base):
    """项目资金台账流水行（仅存手动条目；自动行由收款/付款/费用实时汇总）。"""
    __tablename__ = "project_ledgers"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    entry_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    summary: Mapped[str] = mapped_column(String(200), default="")
    category: Mapped[str] = mapped_column(String(20), default="其他支出", index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)  # 收入正、支出负
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    project: Mapped["Project"] = relationship()


def recalc_stock(db, material: "Material") -> float:
    """重算材料当前库存：stock = 期初 + 入库 - 出库（写操作后调用，保证与流水一致）。"""
    from sqlalchemy import func
    total_in = db.query(func.coalesce(func.sum(InventoryRecord.quantity), 0.0)).filter(
        InventoryRecord.material_id == material.id,
        InventoryRecord.record_type == "入库").scalar() or 0.0
    total_out = db.query(func.coalesce(func.sum(InventoryRecord.quantity), 0.0)).filter(
        InventoryRecord.material_id == material.id,
        InventoryRecord.record_type == "出库").scalar() or 0.0
    material.stock = round((material.opening_stock or 0.0) + float(total_in) - float(total_out), 2)
    return material.stock


class AuditLog(Base):
    """操作审计日志：记录谁在何时对何对象做了什么。"""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    username: Mapped[str] = mapped_column(String(50), default="")
    action: Mapped[str] = mapped_column(String(20), default="", index=True)   # 新增/修改/删除/导入/批量删除
    entity_type: Mapped[str] = mapped_column(String(30), default="", index=True)  # 采购合同/分包合同/发票/付款/材料/采购单
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ==================================================================
# 变更签证 / 增项
# ==================================================================
# 为什么单独建表，而不是在 contracts 上加一个「增项金额」字段：
#
# 装修项目的增项是一笔一笔发生的，每笔都有独立的甲方确认凭证、发生时间、
# 对应成本和结算状态。压成一个字段就只剩一个总数，事后对账时无法回答
# 「这笔增项客户签没签字」「哪个月增的」「增项本身有没有赚钱」。
#
# 金额符号约定（全模块统一，改一处要同步改三处）：
#   amount > 0  增项（甲方多付）
#   amount < 0  减项（合同做小，甲方少付，如取消卫生间）
#   cost   > 0  该变更实际发生的成本（正数）
# 「已确认」才计入结算金额——没签字的变更只是内部意向。

# 状态机（Procore 式：提交 → 业主审签 → 执行 → 结算）
#
# 为什么要多一个「已定价待业主审」
# --------------------------------
# 原来只有「待确认」，但「待确认」同时承担了两件不同的事：
#   · 施工方刚填好金额，还没报给业主
#   · 已经报给业主，等对方签字
# 这两种情况下「还有多少钱在流程里」的含义完全不同——后者是可以拿去
# 催款和计提的数字，前者什么都不是。拆开后待办页才能答出
# 「有多少钱报上去等签字」。
#
# **新状态不计入结算金额**：业主没签字的钱不进账。
# 这个约束由 changes.COUNTED_STATUS 决定，见该文件。
CHANGE_STATUS = [
    "草稿",              # 施工方内部拟稿
    "已定价待业主审",    # 已填金额、已报业主，等签字 —— **不计账**
    "已确认",            # 业主签字 —— 计账
    "已结算",            # 执行完毕、结算完成 —— 计账
    "已驳回",            # 业主否掉 —— 不计账
]
CHANGE_CATEGORIES = ["拆改", "水电", "泥瓦", "木工", "油漆", "安装", "土建", "设计", "其他"]

# 合法流转。UI 与后端都用它，避免出现「已结算 → 草稿」这种倒退。
# key 是当前状态，value 是允许转到的状态集合。
CHANGE_TRANSITIONS = {
    "草稿":            {"已定价待业主审", "已驳回"},
    "已定价待业主审":   {"已确认", "已驳回", "草稿"},   # 退回拟稿 = 业主要求改
    "已确认":          {"已结算", "已驳回"},             # 事后发现签错了也可驳回
    "已结算":          set(),                            # 终态
    "已驳回":          {"草稿"},                         # 重新拟稿再报
}


class ChangeOrder(Base):
    """变更签证单：施工过程中与甲方确认的增减项。"""
    __tablename__ = "change_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    change_no: Mapped[str] = mapped_column(String(50), default="")       # 签证单号
    title: Mapped[str] = mapped_column(String(200), default="")          # 变更事项
    category: Mapped[str] = mapped_column(String(20), default="其他", index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)               # 变更金额（增项正、减项负）
    cost: Mapped[float] = mapped_column(Float, default=0)                 # 对应成本（正数）
    status: Mapped[str] = mapped_column(String(20), default="草稿", index=True)
    raise_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)   # 提出日期
    confirm_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)  # 甲方确认日期
    signer: Mapped[str] = mapped_column(String(50), default="")           # 甲方签字人
    reason: Mapped[str] = mapped_column(Text, default="")                # 变更原因
    note: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")      # 签证单扫描件
    # 扫描件的上传留痕。file_path 存的是 uuid 化的服务器路径，
    # 用户下载看到的是 20261007_a3f9c1e2.pdf 这种名字，对不上他手里的
    # 「领航大厦变更签证001.pdf」；所以另外记原始文件名。
    # 上传人/时间用于「谁在什么时候传了哪份」的回溯——纸质签证单一旦签了
    # 就是法律凭据，日后对账/扯皮时要能说清是哪一版、谁传的。
    file_name: Mapped[str] = mapped_column(String(200), default="")
    file_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    file_uploaded_by: Mapped[str] = mapped_column(String(50), default="")
    # ---- 审批与执行留痕（Procore 式工作流）----
    # 原来「已确认」只是一个按钮，事后只能知道状态变了，不知道谁批的、
    # 为什么批。签字单本身法律凭据，这两个字段是它的数字化对应物。
    submitted_by: Mapped[str] = mapped_column(String(50), default="")        # 谁报的价
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[str] = mapped_column(String(50), default="")         # 业主/监理批准人
    pricing_note: Mapped[str] = mapped_column(String(200), default="")       # 定价依据（一句话）
    reject_reason: Mapped[str] = mapped_column(Text, default="")            # 驳回理由（单列）
    settled_by: Mapped[str] = mapped_column(String(50), default="")         # 谁确认结算完成
    # 执行关联：这笔变更对应哪些采购单/分包单/成本项。存 id 列表（JSON 文本），
    # 而不是建中间表——它们只是「这笔变更花在哪了」的备注性质，
    # 一对多但不需要独立属性（不挂数量、不挂单价）。
    # 转「已结算」时用它做前置校验：关联单据没付清就提示。
    linked_po_ids: Mapped[str] = mapped_column(Text, default="[]")           # 采购单 id
    linked_sc_ids: Mapped[str] = mapped_column(Text, default="[]")           # 分包合同 id
    linked_cost_ids: Mapped[str] = mapped_column(Text, default="[]")        # 成本项 id
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    project: Mapped["Project"] = relationship(back_populates="change_orders")

    # ---------- 关联单据的读写 ----------
    # 存成 JSON 文本而不是真外键：这些关联是「备注性质」的——关联了
    # 不改变采购单本身的任何字段，也不参与数据库层面的约束。
    # 用真外键反而会在删除采购单时留下悬空引用（没有级联语义可定义）。
    @staticmethod
    def _ids_to_json(ids) -> str:
        try:
            clean = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
        except (TypeError, ValueError):
            clean = []
        return json.dumps(sorted(set(clean)), ensure_ascii=False)

    @staticmethod
    def _json_to_ids(raw) -> list:
        if not raw:
            return []
        try:
            v = json.loads(raw)
            return [int(i) for i in v] if isinstance(v, list) else []
        except (json.JSONDecodeError, TypeError, ValueError):
            # 历史上这里也可能被写成逗号分隔（人工改过数据库）
            return [int(p) for p in re.findall(r"\d+", str(raw))]

    @property
    def linked_purchase_orders(self) -> list:
        return self._json_to_ids(self.linked_po_ids)

    @property
    def linked_subcontracts(self) -> list:
        return self._json_to_ids(self.linked_sc_ids)

    @property
    def linked_costs(self) -> list:
        return self._json_to_ids(self.linked_cost_ids)

    @property
    def all_linked_ids(self) -> list:
        return (self.linked_purchase_orders + self.linked_subcontracts
                + self.linked_costs)

    @property
    def can_settle(self) -> bool:
        """是否具备转「已结算」的条件。见 changes.settle_readiness()。

        这里只做一个粗判（有没有关联任何单据），详细的逐项检查在
        changes.settle_readiness()，那里能给出「缺什么」的可读清单。
        之所以在这里也放一个，是为了让模板可以只靠模型就决定
        要不要显示提示，而不必每个页面都去调一次聚合函数。
        """
        return bool(self.all_linked_ids) and (self.cost or 0) > 0

    @property
    def has_scan(self) -> bool:
        """是否已上传签字扫描件。与 status 无关——草稿也能先传，
        确认后再传第二版；是否该催着传由界面按 status 判断。"""
        return bool(self.file_path)

    @property
    def profit(self) -> float:
        """该变更自身的毛利 = 变更金额 − 对应成本。未填成本时返回 0 而非金额，
        否则界面上会把「还没算成本」误显示成 100% 毛利。"""
        if not self.cost:
            return 0.0
        return round(float(self.amount) - float(self.cost), 2)


# ==================================================================
# 报价单（签单漏斗）
# ==================================================================
# 客户状态里已有「已报价」，但没有承载报价内容的对象：报了多少、报给谁、
# 最后成交没有，全在人的记忆里。这里补上漏斗的实体。

QUOTE_STATUS = ["草稿", "已报出", "洽谈中", "已成交", "已流失"]
QUOTE_CATEGORIES = ["主材", "辅材", "人工", "设计", "管理", "拆除", "其他"]


class Quotation(Base):
    """报价单：签约前的报价，与客户绑定。"""
    __tablename__ = "quotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    quote_no: Mapped[str] = mapped_column(String(50), default="")
    title: Mapped[str] = mapped_column(String(200), default="")       # 报价单名称，如「领航大厦 1201 精装」
    community: Mapped[str] = mapped_column(String(100), default="")   # 小区/楼盘
    area: Mapped[float] = mapped_column(Float, default=0)             # 面积(㎡)
    requirement_type: Mapped[str] = mapped_column(String(20), default="")  # 整装/半包/全包
    unit_price: Mapped[float] = mapped_column(Float, default=0)       # 综合单价（元/㎡），便于横向比价
    discount: Mapped[float] = mapped_column(Float, default=100)       # 折扣（%），100 为不打折
    status: Mapped[str] = mapped_column(String(20), default="草稿", index=True)
    quote_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(Date, nullable=True)  # 报价有效期
    lost_reason: Mapped[str] = mapped_column(String(200), default="")  # 流失原因（用于复盘签单率）
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)  # 成交后关联项目
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    customer: Mapped["Customer"] = relationship(back_populates="quotations")
    project: Mapped["Project | None"] = relationship()
    items: Mapped[list["QuotationItem"]] = relationship(
        back_populates="quotation", cascade="all, delete-orphan", order_by="QuotationItem.sort_order")

    @property
    def total(self) -> float:
        """报价总额。优先取明细合计——明细是逐项报价的事实来源；
        没有明细时回落到「面积 × 单价 × 折扣」，让只有面积和单价的
        快速报价也能进漏斗，不至于因为没有明细就录不进来。"""
        items_total = sum(float(i.amount or 0) for i in self.items)
        if self.items:
            return round(items_total, 2)
        if self.area and self.unit_price:
            return round(self.area * self.unit_price * (self.discount or 100) / 100, 2)
        return round(items_total, 2)


class QuotationItem(Base):
    """报价单明细行。"""
    __tablename__ = "quotation_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    quotation_id: Mapped[int] = mapped_column(ForeignKey("quotations.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default="其他", index=True)
    item_name: Mapped[str] = mapped_column(String(200), default="")
    qty: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(20), default="项")
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0)      # 该行金额（可手工覆盖，否则 qty×unit_price）
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")

    quotation: Mapped["Quotation"] = relationship(back_populates="items")


# ==================================================================
# 开票申请（销项）
# ==================================================================
# 现状：invoices 表 41 张全是进项，0 张销项关联到项目。向客户开票
# （尤其建筑业预收款/进度款开票）没有流程，于是出现两个后果：
#   1. 收了 1376 万却查不到开了多少票，对外说不清
#   2. 已开票额无法与收款节点核对，可能超开
#
# 一条记录 = 一次「向某个客户开多少税」的申请单，含申请→开具→作废的状态流转。

INVOICE_APPLICATION_STATUS = ["待开票", "已开票", "部分开票", "已作废", "已驳回"]


class InvoiceApplication(Base):
    """销项开票申请：向客户开票的申请单。"""
    __tablename__ = "invoice_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)  # 对应收款节点
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)    # 开出的发票
    title: Mapped[str] = mapped_column(String(200), default="")    # 发票抬头（默认取客户名）
    tax_no: Mapped[str] = mapped_column(String(50), default="")    # 客户税号
    invoice_type: Mapped[str] = mapped_column(String(30), default="增值税普通发票")
    tax_rate: Mapped[float] = mapped_column(Float, default=0)       # 税率%，如 9 / 6
    amount: Mapped[float] = mapped_column(Float, default=0)         # 申请金额（含税）
    invoiced_amount: Mapped[float] = mapped_column(Float, default=0)  # 已开票金额
    status: Mapped[str] = mapped_column(String(20), default="待开票", index=True)
    apply_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)
    finish_date: Mapped[datetime | None] = mapped_column(Date, nullable=True)  # 开票完成日
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    customer: Mapped["Customer"] = relationship()
    project: Mapped["Project | None"] = relationship()
    payment: Mapped["Payment | None"] = relationship()
    # invoices 与 invoice_applications 之间有两条外键（本表的 invoice_id，
    # 与 invoices.app_id 反向指回来），必须显式声明用哪一条，
    # 否则 SQLAlchemy 无法判断 join 条件，启动即 InvalidRequestError。
    invoice: Mapped["Invoice | None"] = relationship(
        foreign_keys="InvoiceApplication.invoice_id")

    @property
    def tax_excl(self) -> float:
        """不含税金额 = 含税金额 ÷ (1 + 税率/100)。"""
        rate = float(self.tax_rate or 0)
        if rate <= 0:
            return round(float(self.amount or 0), 2)
        return round(float(self.amount or 0) / (1 + rate / 100), 2)

    @property
    def tax_amount(self) -> float:
        return round(float(self.amount or 0) - self.tax_excl, 2)

    @property
    def remain(self) -> float:
        """待开票金额 = 申请额 − 已开票额。"""
        return round(float(self.amount or 0) - float(self.invoiced_amount or 0), 2)
