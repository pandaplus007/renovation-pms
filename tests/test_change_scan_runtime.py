"""变更签证「打印签字单 + 上传扫描件」的端到端验证。

要证明的不是「代码写完了」，而是这三条：

  1. **纸质多联单真的能出纸** —— 页数 = 联数，每联一份，金额带中文大写，
     裁切线在打印时出现、屏幕上不出现（屏幕上画裁切线只是干扰）。
  2. **扫描件可传、可下载、留痕完整** —— 上传后 file_path/file_name/
     uploaded_at/uploaded_by 四项齐全，审计日志里有「谁传的、哪个文件」。
  3. **项目档案统一管理** —— 项目变更页能一眼列出所有扫描件，
     且标出「已确认但缺扫描件」的漏项。

第 2 条会用真文件上传（造一个临时 PNG），跑完删掉，避免污染数据。
"""
from __future__ import annotations

import atexit
import io
import os
import re
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.database import SessionLocal  # noqa: E402
from app.models import ChangeOrder  # noqa: E402

BASE = "http://127.0.0.1:8018"
OUT = os.path.join(ROOT, "tests", "shots")
os.makedirs(OUT, exist_ok=True)
TMP_PNG = os.path.join(OUT, "_tmp_scan.png")
# 一个最小的合法 PNG（1x1 像素）。真实扫描件有几百 KB，测试里没必要。
PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a4944415478da6364000002000100ffff03000006"
    "00057b0d1b0000000049454e44ae426082")

P, F = [], []


def check(name, cond, extra=""):
    (P if cond else F).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + (("  -> " + str(extra)) if extra and not cond else ""))


FIXTURE_NO = "BS-TEST-001"

db = SessionLocal()
# 先清掉历次崩溃遗留的夹具。早先这个清理写在脚本末尾裸执行，
# 一旦中途抛异常（比如 Playwright 超时就抛）就永远执行不到，
# 于是夹具留在库里；下一轮看到「库里有数据」便不再新建、
# 也就不清理——一次崩溃就永久污染，而依赖干净库的其它测试
# （test_newfeat 的变更聚合断言）会随之失败，症状离根因很远。
leftovers = db.query(ChangeOrder).filter(
    ChangeOrder.change_no == FIXTURE_NO).all()
if leftovers:
    for row in leftovers:
        print("清理历次崩溃遗留的夹具 #%d" % row.id)
        db.delete(row)
    db.commit()

objs = db.query(ChangeOrder).order_by(ChangeOrder.id).all()
# 数据库里可能一条变更签证都没有（功能刚做完还没录数据）。此时造一条
# 临时单子跑完全程再删掉——否则这测试在「还没人用过」时永远无法运行，
# 而那恰恰是最需要它兜底的状态。
fixture = None
if not objs:
    from app.models import Project
    proj = db.query(Project).order_by(Project.id).first()
    if proj is None:
        print("连项目都没有，无法构造夹具")
        sys.exit(1)
    fixture = ChangeOrder(
        project_id=proj.id, change_no=FIXTURE_NO,
        title="【测试夹具】临时变更签证单", category="其他",
        amount=12345.67, cost=8000.0, status="已确认",
        reason="自动化测试用，跑完删除",
    )
    db.add(fixture)
    db.commit()
    print("数据库无变更签证，已创建临时夹具 #%d（跑完删除）" % fixture.id)
    objs = [fixture]

target = objs[0]
print("目标签证单：#%d %s  金额 %s  状态 %s"
      % (target.id, target.title, target.amount, target.status))
print("上传前：file_path=%r uploaded_by=%r"
      % (target.file_path, target.file_uploaded_by))
db.close()


def _cleanup_fixture():
    """删掉夹具。用 atexit 注册而非写在脚本末尾——

    写成末尾的裸语句时，中途抛异常（Playwright 超时很常见）就跳过了，
    夹具留在库里。下一轮运行看到「库里有数据」便不再新建、也就不清理，
    于是脏数据永久存在；而依赖干净库的其它测试（test_newfeat 的变更
    聚合断言）会随之失败，症状离根因十万八千里。一次崩溃污染到底，
    排查成本远高于现在多写这几行。
    """
    try:
        d = SessionLocal()
        row = d.query(ChangeOrder).filter(
            ChangeOrder.change_no == FIXTURE_NO).first()
        if row is not None:
            d.delete(row)
            d.commit()
            print("\n已清理临时夹具 #%d" % row.id)
        d.close()
    except Exception as e:
        print("\n!! 清理夹具失败：%s" % e)


atexit.register(_cleanup_fixture)

with open(TMP_PNG, "wb") as f:
    f.write(PNG_1x1)

with sync_playwright() as pw:
    b = pw.chromium.launch()
    ctx = b.new_context(viewport={"width": 1400, "height": 950})
    pg = ctx.new_page()
    pg.goto(BASE + "/login")
    pg.fill("input[name=username]", "admin")
    pg.fill("input[name=password]", "admin123")
    pg.click("button[type=submit]")
    pg.wait_for_load_state("networkidle")

    print()
    print("=" * 76)
    print("[1] 打印页：联数、金额大写、裁切线")
    print("=" * 76)
    for copies in (1, 3, 5):
        pg.goto(BASE + "/changes/%d/print?copies=%d" % (target.id, copies))
        pg.wait_for_load_state("networkidle")
        pg.wait_for_timeout(250)
        sheets = pg.eval_on_selector_all(".sheet", "e => e.length")
        tags = pg.eval_on_selector_all(
            ".copy-tag", "e => e.map(x => x.textContent.trim())")
        check("copies=%d -> %d 联" % (copies, sheets), sheets == copies,
              sheets)
        if copies == 3:
            print("      联次标签：%s" % tags)
            check("联次标明用途（甲方/乙方/现场）",
                  all(any(k in t for k in ("甲方", "乙方", "现场"))
                      for t in tags), tags)
            # 金额中文大写
            cn = pg.eval_on_selector(".cn-amount", "e => e.textContent.trim()")
            print("      金额大写：%s" % cn)
            check("金额带中文大写且非空",
                  bool(cn) and "元" in cn, cn)
            check("大写金额不是阿拉伯数字",
                  not re.fullmatch(r"[-−\d,.\s￥元角整整]*", cn or "x")
                  or not re.match(r"^[-−\d]", cn or ""), cn)
            # 签字栏
            boxes = pg.eval_on_selector_all(".sign-box", "e => e.length")
            check("每联都有 3 个签字栏（甲方/乙方/见证）", boxes == copies * 3,
                  boxes)
            # 屏幕状态下裁切线必须隐藏
            cut_screen = pg.eval_on_selector(
                ".cut-line", "e => getComputedStyle(e).display")
            check("屏幕上不显示裁切线（只干扰预览）",
                  cut_screen == "none", cut_screen)
            # 打印状态下必须出现
            pg.emulate_media(media="print")
            pg.wait_for_timeout(200)
            cut_print = pg.eval_on_selector(
                ".cut-line", "e => getComputedStyle(e).display")
            check("打印时显示裁切线", cut_print != "none", cut_print)
            bars = pg.eval_on_selector_all(
                ".progress-bar", "e => e.length")
            check("打印页不含应用外壳的进度条（应 0）", bars == 0, bars)
            # PDF 必须在 print 媒体下生成。之前这里先 emulate_media("screen")
            # 再调 page.pdf()，出的是屏幕媒体的效果——裁切线不显示、
            # 也没有 page-break，页数完全不对。是个很容易重犯的坑。
            pg.pdf(path=os.path.join(OUT, "change_print.pdf"), format="A4",
                   print_background=True)
            pg.emulate_media(media="screen")

    # PDF 页数必须等于联数
    try:
        import fitz
        doc = fitz.open(os.path.join(OUT, "change_print.pdf"))
        print("      PDF 页数：%d" % doc.page_count)
        check("PDF 页数 = 3 联", doc.page_count == 3, doc.page_count)
        txt = doc[0].get_text()
        print("      PDF 首页文字含「变更签证单」：%s" % ("变更签证单" in txt))
        check("PDF 里有中文（不是方块）", "变更签证单" in txt, txt[:120])
        doc.close()
    except ImportError:
        print("      (无 PyMuPDF，跳过 PDF 页数校验)")

    print()
    print("=" * 76)
    print("[2] 编辑页：上传扫描件")
    print("=" * 76)
    pg.goto(BASE + "/changes/%d/edit" % target.id)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(250)
    check("编辑页无渲染错误", "Traceback" not in pg.inner_text("body"))
    check("有上传表单", pg.locator('input[name=file]').count() == 1)
    check("有打印入口", pg.locator('a[href*="/print?copies=3"]').count() >= 1)

    pg.set_input_files('input[name=file]', TMP_PNG)
    pg.click('form[action*="/upload"] button')
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(400)
    txt = pg.inner_text("body")
    check("上传后有成功提示", "扫描件已上传" in txt,
          [x for x in txt.split("\n") if "扫描件" in x][:3])

    db = SessionLocal()
    o = db.get(ChangeOrder, target.id)
    print("      file_path=%s" % o.file_path)
    print("      file_name=%s" % o.file_name)
    print("      uploaded_at=%s  by=%s" % (o.file_uploaded_at, o.file_uploaded_by))
    check("file_path 已写入", bool(o.file_path), o.file_path)
    check("file_name 记录了原始文件名", bool(o.file_name), o.file_name)
    check("记录了上传时间", o.file_uploaded_at is not None)
    check("记录了上传人", bool(o.file_uploaded_by), o.file_uploaded_by)
    check("has_scan 变为 True", o.has_scan is True)
    p = o.file_path
    db.close()

    # 下载校验
    r = ctx.request.get(BASE + p)
    check("扫描件可下载（HTTP %s）" % r.status, r.status == 200, r.status)
    check("下载内容非空", len(r.body()) > 0, len(r.body()))

    # 审计日志
    pg.goto(BASE + "/oa/audit")
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    atxt = pg.inner_text("body")
    check("审计日志记了「上传扫描件」", "上传扫描件" in atxt,
          [x for x in atxt.split("\n") if "上传" in x][:3])
    check("审计日志记了原始文件名", "_tmp_scan.png" in atxt,
          [x for x in atxt.split("\n") if "tmp_scan" in x][:2])

    print()
    print("=" * 76)
    print("[3] 拒绝不支持的文件类型")
    print("=" * 76)
    bad = os.path.join(OUT, "_tmp_scan.txt")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("not an image")
    pg.goto(BASE + "/changes/%d/edit" % target.id)
    pg.wait_for_load_state("networkidle")
    pg.set_input_files('input[name=file]', bad)
    pg.click('form[action*="/upload"] button')
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    t2 = pg.inner_text("body")
    check("txt 被拒绝并给出提示", "不支持的文件类型" in t2,
          [x for x in t2.split("\n") if "不支持" in x][:2])
    db = SessionLocal()
    check("拒绝后 file_path 未被污染", bool(db.get(ChangeOrder, target.id).file_path))
    db.close()
    os.remove(bad)

    print()
    print("=" * 76)
    print("[4] 项目档案：扫描件汇总")
    print("=" * 76)
    pg.goto(BASE + "/projects/%d/changes" % target.project_id)
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(300)
    t3 = pg.inner_text("body")
    check("项目变更页无渲染错误", "Traceback" not in t3)
    check("有「签字扫描件」汇总区块", "签字扫描件" in t3,
          [x for x in t3.split("\n") if "扫描件" in x][:4])
    check("汇总里列出了上传留痕", "上传留痕" in t3)
    check("汇总统计了 已上传 N / M", "已上传" in t3,
          [x for x in t3.split("\n") if "已上传" in x][:2])
    heads = pg.eval_on_selector_all(
        "table thead th", "e => e.map(x => x.innerText.trim())")
    print("      表头：%s" % heads)
    check("明细表有「扫描件」列", "扫描件" in heads, heads)
    check("汇总表有「上传留痕」列", "上传留痕" in heads, heads)
    pg.screenshot(path=os.path.join(OUT, "scan_summary.png"), full_page=True)

    print()
    print("=" * 76)
    print("[5] 解除关联：只清关联，磁盘文件保留")
    print("=" * 76)
    pg.goto(BASE + "/changes/%d/edit" % target.id)
    pg.wait_for_load_state("networkidle")
    pg.on("dialog", lambda d: d.accept())
    pg.click('form[action*="/scan-delete"] button')
    pg.wait_for_load_state("networkidle")
    pg.wait_for_timeout(400)
    db = SessionLocal()
    o = db.get(ChangeOrder, target.id)
    check("file_path 已清空", not o.file_path, o.file_path)
    check("file_name 已清空", not o.file_name, o.file_name)
    db.close()
    # 磁盘文件必须还在——扫描件是凭据，不能被一个按钮删掉
    disk = os.path.join(ROOT, "uploads", p.replace("/uploads/", "").replace("/", os.sep))
    check("磁盘上的扫描件仍保留", os.path.exists(disk), disk)

    ctx.close()
    b.close()

os.remove(TMP_PNG)

print()
print("=" * 76)
print("通过 %d 项，失败 %d 项" % (len(P), len(F)))
for x in F:
    print("  失败：" + x)
print("截图与 PDF：", OUT)
print("=" * 76)
sys.exit(1 if F else 0)