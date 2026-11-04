"""视觉识别自检脚本：验证转账单 / 扫描件合同的自动识别链路。

用途：换平台、换 key、换模型之后，先用它确认链路通，再去界面上传真实单据。
它直接把失败原因打出来，避免再去猜“为什么识别不了”。

用法（在项目根目录执行）：
    python scripts/test_vision.py                 # 诊断配置与连通性
    python scripts/test_vision.py --list-models   # 列出当前平台可用模型，便于选 VISION_MODEL
    python scripts/test_vision.py --sample        # 生成一张合成回单图片并试识别（打印字段比对）
    python scripts/test_vision.py --image 某回单.png
    python scripts/test_vision.py --contract docs/某合同.pdf

说明：--sample 生成的测试图保存在下载文件夹（Windows 为 C:\\Users\\<用户>\\Downloads）。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows 控制台默认 GBK，会因 emoji / 特殊符号直接抛 UnicodeEncodeError，
# 统一改成 UTF-8 输出（失败时退回占位符，不影响识别逻辑）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from app.utils.vision_client import (  # noqa: E402
    PROVIDERS, diagnose, list_models, resolve_config,
)


def _mask(key: str) -> str:
    if not key:
        return "(空)"
    return key[:6] + "****" + key[-4:] if len(key) > 12 else key[:3] + "****"


def show_config() -> None:
    cfg, err = resolve_config()
    print("— 当前识别配置 —")
    if not cfg:
        print("  未就绪：" + err)
        print("  可选平台：" + " / ".join(f"{k}（{v['label']}）" for k, v in PROVIDERS.items()))
        return
    print(f"  平台      ：{cfg['provider']}（{cfg['label']}）")
    print(f"  协议      ：{cfg['protocol']}")
    print(f"  接口地址  ：{cfg['base_url']}")
    print(f"  模型      ：{cfg['model']}")
    print(f"  API Key   ：{_mask(cfg['api_key'])}")


def cmd_diagnose() -> int:
    show_config()
    print("\n— 连通性诊断 —")
    ok, msg = diagnose()
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        print("\n提示：额度/鉴权类问题需要到对应平台后台处理；模型名不对则改 .env 的 VISION_MODEL。")
        print("      项目根目录 .env 已列出各平台填写模板。")
    return 0 if ok else 1


def cmd_list_models() -> int:
    show_config()
    print("\n— 平台可用模型 —")
    ids, err = list_models()
    if err:
        print("  ❌ " + err)
        return 1
    if not ids:
        print("  （平台未返回模型列表，可继续用当前 VISION_MODEL）")
        return 1
    for i in ids:
        mark = " ← 当前使用" if i == (resolve_config()[0] or {}).get("model") else ""
        print(f"  {i}{mark}")
    return 0


def make_sample(path: str) -> str:
    """生成一张合成银行回单图片，用于验证“图片→模型→JSON→字段”整条链路。"""
    from PIL import Image, ImageDraw, ImageFont

    lines = [
        ("中国工商银行  电子转账回单", 30),
        ("", 12),
        ("付款方户名：杭州云筑装饰工程有限公司", 24),
        ("付款方账号：1202020009001234567", 24),
        ("付款方开户行：工商银行杭州文三路支行", 24),
        ("收款方户名：李建国", 24),
        ("收款方账号：6222021201008888999", 24),
        ("收款方开户行：建设银行上海浦东支行", 24),
        ("交易金额：￥128,500.00", 26),
        ("交易日期：2026-09-18", 24),
        ("摘要：一期工程进度款", 24),
    ]
    font_path = r"C:\Windows\Fonts\msyh.ttc"
    if not os.path.exists(font_path):
        font_path = r"C:\Windows\Fonts\simhei.ttf"

    def font(size: int):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            return ImageFont.load_default()

    img = Image.new("RGB", (760, 560), "white")
    draw = ImageDraw.Draw(img)
    y = 30
    for text, size in lines:
        if text:
            draw.text((40, y), text, fill="black", font=font(size))
        y += size + 16
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path)
    return path


def cmd_sample() -> int:
    show_config()
    out = os.path.join(os.path.expanduser("~"), "Downloads", "vision-test-receipt.png")
    make_sample(out)
    print(f"\n已生成测试回单图片：{out}")
    return run_bank(out)


EXPECTED = {
    "payer": "杭州云筑装饰工程有限公司",
    "payee": "李建国",
    "amount": 128500.0,
    "trade_date": "2026-09-18",
    "summary": "一期工程进度款",
}


def run_bank(path: str) -> int:
    from app.utils.bank_parser import recognize_bank_file

    with open(path, "rb") as f:
        data = f.read()
    print("\n— 转账单识别 —")
    parsed, err = recognize_bank_file(os.path.basename(path), data)
    if not parsed:
        print("  ❌ " + err)
        return 1
    for k in ("payer", "payee", "payer_account", "payee_account",
              "payer_bank", "payee_bank", "amount", "trade_date", "summary"):
        print(f"  {k:<14}: {parsed.get(k)}")
    # 合成样张才做逐字段断言；真实单据只展示结果，由人工判断准确率
    if os.path.basename(path) == "vision-test-receipt.png":
        print("\n— 期望值比对 —")
        bad = 0
        for k, want in EXPECTED.items():
            got = parsed.get(k)
            ok = str(got) == str(want)
            print(f"  {'✅' if ok else '⚠️ '} {k:<12} 期望 {want} / 实际 {got}")
            bad += 0 if ok else 1
        print("\n链路已通。" if bad == 0 else f"\n链路已通，但有 {bad} 个字段与期望不一致（属模型精度问题，可换更强的视觉模型）。")
    return 0


def cmd_contract(path: str) -> int:
    from app.utils.contract_parser import recognize_contract_file

    with open(path, "rb") as f:
        data = f.read()
    print("\n— 合同识别 —")
    parsed, err = recognize_contract_file(os.path.basename(path), data)
    if not parsed:
        print("  ❌ " + err)
        return 1
    for k in ("contract_no", "name", "party_a", "party_b", "amount", "sign_date"):
        print(f"  {k:<12}: {parsed.get(k)}")
    print(f"  时间节点    : {len(parsed.get('milestones', []))} 个")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="视觉识别自检（转账单 / 扫描件合同）")
    ap.add_argument("--list-models", action="store_true", help="列出当前平台可用模型")
    ap.add_argument("--sample", action="store_true", help="生成合成回单并试识别")
    ap.add_argument("--image", metavar="PATH", help="用指定的转账单图片试识别")
    ap.add_argument("--contract", metavar="PATH", help="用指定的合同文件试识别")
    args = ap.parse_args()

    if args.list_models:
        return cmd_list_models()
    if args.sample:
        return cmd_sample()
    if args.image:
        show_config()
        return run_bank(args.image)
    if args.contract:
        show_config()
        return cmd_contract(args.contract)
    return cmd_diagnose()


if __name__ == "__main__":
    sys.exit(main())
