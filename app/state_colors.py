"""统一状态色：把散落 20+ 模板的 31 种徽章写法收敛成一套字典。

为什么现在要做
--------------
审计发现项目状态徽章在 4 个文件里各写了一遍字面量完全相同的表达式：

    text-bg-{{ 'success' if p.status=='已验收' else 'primary' if p.status=='施工中'
               else 'warning text-dark' if p.status in ['待签约','待开工'] else 'secondary' }}

dashboard.html / projects/detail.html / projects/form.html / contracts/detail.html
四处一模一样。改一次配色要动 4 个文件，漏一处就不一致——这比"颜色多"
更难察觉，也更难维护。

语义收敛成 4 档
----------------
    完成   success  已验收 / 已确认 / 已结清 / 已收 / 正常 / 启用 / 匹配上
    推进   primary   施工中 / 审批中 / 已报出 / 已下单 / 进项  ← 品牌绿，"在动"
    关注   warning   待确认 / 部分到货 / 逾期 / 未跟进 / 部分已收
    中性   secondary 草稿 / 待签约 / 待开工 / 已流失 / 已作废 / 停用 / 已驳回

为什么不留第 5 个「失败红」
--------------------------
「danger」在现有代码里只用于两种情况：已驳回、金额超付。前者归入中性
（驳回是流程内的正常终止，不是错误），后者是真正的异常——
所以 danger 保留但**只用于金额异常**，其余业务状态不碰。

这样做的代价是「已驳回」不再是红色。收益是：红色出现 = 有真问题，
扫一眼就知道。这是财务系统该有的信号强度——红色用滥了就等于没有。

为什么不用「红=错、绿=对」这套直觉
--------------------------------
因为这套系统里 success 和 primary 都是绿系（#10b981 / #3d7a63），
色相接近而饱和度差 3 倍。真要靠颜色区分「已完成」和「施工中」，
色盲用户（占比约 8% 男性）会看到两个几乎一样的绿。
所以：**颜色只表达 4 档粗粒度，精确状态靠徽章上的文字**。
每个徽章都有字，不存在"只能靠颜色识别"的情况。
"""
from __future__ import annotations

# 4 档语义色。用 dict 而不是函数，是为了在模板里能直接 STATE_BADGE.get(x)
STATE_TONE = {
    "done": "success",      # 已完成 / 已确认 / 正常
    "active": "primary",    # 正在推进
    "watch": "warning",     # 需要关注
    "idle": "secondary",    # 未开始 / 已终止
    "alert": "danger",      # 仅金额异常
}

# 状态词 → 语义档。**全站唯一来源**，模板不再各写各的。
# 键是业务状态词，值是 STATE_TONE 的键。
STATE_TONE_MAP: dict[str, str] = {
    # ---- 项目 ----
    "已验收": "done",
    "售后": "done",
    "施工中": "active",
    "待开工": "idle",
    "待签约": "idle",
    "待验收": "watch",
    # ---- 审批 ----
    "已通过": "done",
    "审批中": "active",
    "待审批": "watch",
    "已驳回": "idle",
    "已撤回": "idle",
    # ---- 变更签证 ----
    "已确认": "done",
    "已结算": "done",
    # 「已定价待业主审」用 watch 而不是 active：钱已经报出去了，
    # 球在业主那边，我方唯一能做的就是催——这正是「需要关注」，
    # 不是「正在推进」。用 active 会让人误以为系统里有进展。
    "已定价待业主审": "watch",
    "待确认": "watch",
    "草稿": "idle",
    # ---- 报价 ----
    "已成交": "done",
    "洽谈中": "active",
    "已报出": "active",
    "已流失": "idle",
    # ---- 开票申请 ----
    "已开票": "done",
    "部分开票": "watch",
    "待开票": "watch",
    "已作废": "idle",
    # ---- 客户 ----
    "线索": "idle",
    "已联系": "active",
    "已量房": "active",
    "已报价": "active",
    "已签约": "done",
    # ---- 采购 / 配送 ----
    "已到货": "done",
    "已下单": "active",
    "部分到货": "watch",
    "待下单": "idle",
    "已送货": "done",
    "未送货": "watch",
    "无需送货": "idle",
    # ---- 发票 ----
    "正常": "done",
    "进项": "active",
    "销项": "active",
    "作废": "idle",
    # ---- 收款（客户 → 公司，所以是「收」不是「付」）----
    # 付款侧（公司 → 供应商）用的是「已结清 / 部分结算 / 未付款」，
    # 与这三个词不重叠，所以本节三个键由 Payment.status 独占。
    "已收": "done",
    "部分已收": "watch",
    "未收": "idle",
    # ---- 施工阶段（ProjectPhase.status / GanttTask.status）----
    # 这几个词一直是「施工阶段」的三档状态，但从来没进过这张表——
    # 于是 `{{ ph.status | state_badge }}` 全部静默回落成中性灰，
    # 「已完成」和「未开始」长得一模一样。2026-10 改版工程管理页时补上。
    # 「施工中」不在这里重复定义：它上面「项目」那一节已经有了，
    # 项目状态和阶段状态共用同一个词，同档同色（active）。
    # 「待开始」是自动生成阶段的写法，与「未开始」同档。
    "未开始": "idle",
    "待开始": "idle",
    "进行中": "active",
    "已完成": "done",
    # ---- 通用 ----
    "启用": "done",
    "停用": "idle",
    "启用中": "done",
    "未启用": "idle",
    "内置": "idle",
    "超付": "alert",
    "结清": "done",
    "已结清": "done",
    "当前": "active",
    "已导入": "done",
    "已上传存档": "done",
    "已上传附件": "done",
    "未提交": "idle",
    "待启动": "watch",
    "已驳回过": "idle",
    # ---- 现场记录问题闭环 ----
    "待处理": "watch",
    "处理中": "active",
    "已解决": "done",
    "未超付": "done",
    # ---- 银行对账匹配结果 ----
    "匹配上": "done",
    "流水有、系统没": "watch",
    "系统有、流水没": "watch",
    # ---- 客户跟进 ----
    # 逾期/未跟进是 watch 而非 alert：跟进逾期要人去推一把，
    # 但它不是「钱错了」。红色留给金额异常，否则红色一多就等于没有。
    "逾期": "watch",
    "未跟进": "watch",
    "已启用": "done",
}


def badge_class(word, *, extra: str = "") -> str:
    """状态词 → 徽章 class。找不到时回落到中性档，绝不抛异常。

    模板里统一用 `{{ word | state_badge }}`，避免各页面重新发明映射。

    为什么全程容错：word 来自数据库字段，可能是 None、空串，
    也可能是这个字典建立之后新加的状态词。任何一种都不该让整页 500——
    徽章只是展示，猜错了显示成中性灰即可。

    extra 参数是关键字参数；Jinja 过滤器不会传它，但测试与个别调用方会用。
    这里显式 str() 一次，因为 Jinja 在某些版本下会把多参数过滤器
    传成 tuple，str() 顺手把这个坑也堵上。
    """
    extra = str(extra or "")
    tone = STATE_TONE_MAP.get(word if isinstance(word, str) else "", "idle")
    return ("state-badge state-%s" % tone + (" " + extra if extra else "")).strip()


def tone_badge(tone) -> str:
    """档位名 → 完整徽章 class。给「不是状态词」的场合用（如发票方向）。

    与 badge_class 的区别是**契约不同**，两者不能混用：

      badge_class  接的是**业务状态词**（'施工中'），查 STATE_TONE_MAP 决定档位，
                   查不到就回落到 idle —— 因为词是数据，不该让页面炸掉。
      tone_badge   接的是**档位名**（'active'），直接用，不查字典。

    这也是为什么这里必须显式报错而不是回落：档位名是**写死在模板里的常量**，
    拼错（写成 'state-active' 少了前缀、大小写错）属于代码 bug，
    应该立刻炸出来。若它也悄悄回落到 idle，销项和进项会一起变灰——
    方向信息整个丢失，而且界面上看不出任何异常。

    历史教训：早先的 state_tone_class 收 'state-active' 却拿它去查状态词
    字典，永远 miss、静默回落 idle，同时漏掉 .state-badge 前缀，
    于是白字画在透明背景上。整个徽章不可见，而自动检查把透明当黑色
    算成 21:1 对比度，反而判为通过——两个 bug 叠在一起互相掩护。
    """
    name = str(tone or "").strip()
    if name not in STATE_TONE:
        raise ValueError(
            "未知档位 %r，可用：%s。若这里传的是业务状态词，"
            "请改用 {{ 状态词 | state_badge }}。"
            % (tone, "/".join(STATE_TONE)))
    return "state-badge state-%s" % name


# Jinja 过滤器：{{ '施工中' | state_badge }} → 'state-badge state-active'
def register(env) -> None:
    env.filters["state_badge"] = badge_class
    env.filters["tone_badge"] = tone_badge
    env.globals["STATE_TONE"] = STATE_TONE
    env.globals["STATE_TONE_MAP"] = STATE_TONE_MAP
    # 直接给整个字典，让模板能一次拿到「状态词 → 徽章类」
    env.globals["STATE_BADGE"] = {
        w: badge_class(w) for w in set(STATE_TONE_MAP)
    }
    # 状态词 → 档位，模板里偶尔需要直接判断（如按状态分组排序）
    env.globals["state_tone"] = lambda w: STATE_TONE_MAP.get(
        w if isinstance(w, str) else "", "idle")
    # 同一件事的过滤器写法：`{{ '已完成' | state_tone }}`。
    # 两种写法都要有——只注册 globals 的话，模板里写成过滤器形式会报
    # 「No filter named 'state_tone'」，而且 Jinja 在**渲染时**才报，
    # env.parse() 编译期查不出来（这次就是这么漏掉的）。
    env.filters["state_tone"] = env.globals["state_tone"]
    
    # 兼容别名：这些变量名历史上由各路由自己传入（sc / psc / status_badge），
    # 改一处要动 4 个路由。现在统一从全局注入，模板与路由都不必再构造。
    # 未知状态词回落到 idle（中性灰）——不抛异常、不显示成红色。
    env.globals["sc"] = env.globals["STATE_BADGE"]
    env.globals["psc"] = env.globals["STATE_BADGE"]
    env.globals["status_badge"] = env.globals["STATE_BADGE"]