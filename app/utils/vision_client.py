"""视觉识别客户端：可切换第三方视觉模型，统一返回 (JSON 结果, 错误原因)。

设计目标
- 不再绑定单一平台：Proma Cloud 额度用尽时，换一家平台只改 .env，不动业务代码
- 失败原因必须可见：401/402/429/超时/额度不足 都要能把真实原因回传到界面，
  而不是统一变成“未能识别（可能是扫描件）”——后者会把配置问题伪装成文件问题

支持两类接口协议
- openai：OpenAI 兼容的 /chat/completions（图片走 image_url + data URL）
  适用于阿里云百炼、智谱、硅基流动、火山方舟、Kimi、OpenAI 及任何自定义 base_url
- anthropic：Anthropic Messages 协议的 /v1/messages（图片走 source.base64）
  保留给 Proma Cloud 旧配置，便于随时切回

配置（.env 或环境变量，见 config.py）
- VISION_PROVIDER  平台标识（见下方 PROVIDERS），留空时按已有 key 自动判断
- VISION_API_KEY   平台 API Key
- VISION_BASE_URL  自定义 base_url（provider=custom 时必填）
- VISION_MODEL     模型名，留空用平台默认值
"""
import base64
import json
import re
import time

import requests

from ..config import (
    PROMACLOUD_API_KEY,
    VISION_API_KEY,
    VISION_BASE_URL,
    VISION_MODEL,
    VISION_PROVIDER,
)

# 各平台默认接入参数。模型名以平台文档为准：智谱 glm-4.6v-flash 已于 2026-09-22
# 核对官方价格页与模型概览（官方免费视觉模型）。其余平台的默认模型是常见 ID，
# 平台上调/下线模型时会失效——用 scripts/test_vision.py --list-models 可列出当前可用
# 模型，diagnose 也会在模型不存在时给出相近候选。
PROVIDERS = {
    "dashscope": {
        "label": "阿里云百炼（通义千问）",
        "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-vl-max",
    },
    "zhipu": {
        "label": "智谱 AI（GLM 视觉）",
        "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # 代码默认用官方免费视觉模型 glm-4.6v-flash（128K 上下文 / 最大输出 32K）。
        # 实测：免费模型高峰时段频繁返回 429（2026-09-23 连测 2 次均 429），
        # 本项目 .env 已改为付费轻量版 glm-4.6v-flashx（实测单张回单约 0.0006 元）。
        # 默认值故意保持免费版：避免未显式配置时静默产生费用。
        "model": "glm-4.6v-flash",
        # 关闭思考模式（官方参数，见 docs.bigmodel.cn/cn/guide/capabilities/thinking-mode）。
        # GLM-4.6 系列默认自动开启 thinking，实测同一张回单：
        #   开启思考：输出 1683 tokens（其中思维 1427），且 max_tokens 偏小时会被思维链吃光、
        #            返回 finish_reason=length 与空内容（表现为“识别失败”）
        #   关闭思考：输出 245 tokens，返回内容完全一致
        # 识别字段这种结构化抽取不需要长思维链，关掉更稳、更便宜（约 1/6.9 tokens）。
        # 注意：GLM-5.3 / GLM-5.3-Flash 强制思考、无法关闭，换模型时需一并去掉此项。
        "extra_body": {"thinking": {"type": "disabled"}},
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
    },
    "ark": {
        "label": "火山方舟（豆包视觉）",
        "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-1.5-vision-pro-32k",
    },
    "moonshot": {
        "label": "Kimi 月之暗面",
        "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k-vision-preview",
    },
    "openai": {
        "label": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "promacloud": {
        "label": "Proma Cloud（旧配置）",
        "protocol": "anthropic",
        "base_url": "https://api.proma.cool",
        "model": "claude-sonnet-5",
    },
    "custom": {
        "label": "自定义 OpenAI 兼容平台",
        "protocol": "openai",
        "base_url": "",
        "model": "",
    },
}


# 图片真实类型 → 视觉接口需要的 media_type
IMAGE_MEDIA_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


def sniff_kind(data: bytes, filename: str = "") -> str:
    """按文件头判断真实类型，返回 'pdf' / 'jpeg' / 'png' / 'webp' / 'bmp' / ''。

    为什么不信扩展名：手机拍的回单、银行导出的图片经常被存成 .pdf，
    而残缺/占位的 .pdf（如 13 字节空文件）也会进到 uploads 目录，
    只按扩展名分支会直接把这两种情况当成“PDF 无文字层”或解析报错。
    """
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    # 文件头不可辨认时退回扩展名，尽量不做无谓失败
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".jpg", ".jpeg")):
        return "jpeg"
    if lower.endswith(".png"):
        return "png"
    if lower.endswith(".webp"):
        return "webp"
    if lower.endswith(".bmp"):
        return "bmp"
    return ""


def resolve_config() -> tuple[dict | None, str]:
    """解析当前生效的平台配置。返回 (配置字典, 错误说明)；无可用配置时配置为 None。"""
    provider = (VISION_PROVIDER or "").strip().lower()

    # 未显式指定平台时按已有 key 推断，保证老 .env（只有 PROMACLOUD_API_KEY）继续可用
    if not provider:
        if VISION_API_KEY:
            # 有通用 key 但没说平台，无法猜 base_url / 协议
            return None, ("检测到 VISION_API_KEY 但未设置 VISION_PROVIDER，"
                          "请在 .env 指定平台（dashscope / zhipu / siliconflow / ark / "
                          "moonshot / openai / custom）")
        if PROMACLOUD_API_KEY:
            provider = "promacloud"
        else:
            return None, ("未配置视觉识别密钥：请在 .env 设置 VISION_PROVIDER 与 VISION_API_KEY"
                          "（图片回单与扫描件合同需要联网识别；也可手动填写字段）")

    if provider not in PROVIDERS:
        return None, (f"未知的 VISION_PROVIDER：{provider}；"
                      f"可选：{' / '.join(PROVIDERS.keys())}")

    preset = PROVIDERS[provider]
    api_key = VISION_API_KEY or (PROMACLOUD_API_KEY if provider == "promacloud" else "")
    if not api_key:
        return None, (f"未配置 {preset['label']} 的 API Key：请在 .env 设置 VISION_API_KEY"
                      "（可手动填写字段，不影响文件存档）")

    base_url = (VISION_BASE_URL or preset["base_url"]).rstrip("/")
    if not base_url:
        return None, ("VISION_PROVIDER=custom 时必须同时设置 VISION_BASE_URL"
                      "（形如 https://xxx/v1）")

    model = VISION_MODEL or preset["model"]
    if not model:
        return None, "未指定模型：请设置 VISION_PROVIDER 或 VISION_MODEL"

    return {"provider": provider, "label": preset["label"], "protocol": preset["protocol"],
            "base_url": base_url, "model": model, "api_key": api_key,
            "extra_body": preset.get("extra_body") or {}}, ""


def is_configured() -> bool:
    """是否具备可用的视觉识别配置（供界面判断是否提示“未配置”）。"""
    cfg, _ = resolve_config()
    return cfg is not None


def _clean_json(text: str) -> str:
    """剥掉 markdown 代码块与解释文字，取出最外层的 JSON 对象或数组。

    必须同时支持数组：一张截图里含多笔回单时，模型会返回 [{...},{...}]。
    早期实现只取“第一个 { 到最后一个 }”，会把方括号削掉变成非法 JSON。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    s_obj, e_obj = text.find("{"), text.rfind("}")
    s_arr, e_arr = text.find("["), text.rfind("]")
    # 数组在前（或没有对象）时按数组截取
    if s_arr != -1 and e_arr > s_arr and (s_obj == -1 or s_arr < s_obj):
        return text[s_arr:e_arr + 1]
    if s_obj != -1 and e_obj > s_obj:
        return text[s_obj:e_obj + 1]
    return text


def _error_detail(resp: requests.Response) -> str:
    """从各家平台不同形状的错误体里尽量取出人类可读的原因。"""
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:200]
    for path in (("error", "message"), ("detail", "error", "message"),
                 ("detail",), ("message",), ("msg",)):
        node = data
        for k in path:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                node = None
                break
        if isinstance(node, str) and node.strip():
            return node.strip()[:200]
    return json.dumps(data, ensure_ascii=False)[:200]


def _friendly_error(resp: requests.Response, cfg: dict) -> str:
    """把 HTTP 状态翻译成用户能直接理解、并能据此行动的提示。"""
    code = resp.status_code
    detail = _error_detail(resp)
    tag = cfg["label"]
    if code in (401, 403):
        return f"{tag} 鉴权失败（HTTP {code}）：API Key 无效或无权访问该模型。{detail}"
    if code == 402:
        return f"{tag} 账户余额/额度不足（HTTP 402）：{detail}"
    if code == 404:
        return f"{tag} 接口或模型不存在（HTTP 404）：请检查 VISION_BASE_URL 与 VISION_MODEL。{detail}"
    if code == 429:
        return (f"{tag} 触发限流（HTTP 429）：{detail}；"
                f"免费模型高峰时段常见，可稍后重试；也可改用同系列付费轻量版"
                f"（成本极低，识别更稳定）")
    if 400 <= code < 500:
        return (f"{tag} 拒绝本次请求（HTTP {code}）：{detail}"
                f"　当前模型：{cfg['model']}；若提示模型不可用，请改 .env 的 VISION_MODEL")
    if code >= 500:
        return f"{tag} 服务端异常（HTTP {code}）：稍后重试。{detail}"
    return f"{tag} 返回异常（HTTP {code}）：{detail}"


def _build_request(cfg: dict, images: list[tuple[bytes, str]],
                   system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[str, dict, dict]:
    """按协议构造 (url, headers, payload)。images 为 (字节, media_type) 列表。"""
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    if cfg["protocol"] == "anthropic":
        content = [{"type": "image",
                    "source": {"type": "base64", "media_type": mt,
                               "data": base64.b64encode(data).decode()}}
                   for data, mt in images]
        content.append({"type": "text", "text": user_prompt})
        payload = {
            "model": cfg["model"],
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": content}],
        }
        return f"{cfg['base_url']}/v1/messages", headers, payload

    content = [{"type": "image_url",
                "image_url": {"url": f"data:{mt};base64,{base64.b64encode(data).decode()}"}}
               for data, mt in images]
    content.append({"type": "text", "text": user_prompt})
    payload = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system_prompt},
                     {"role": "user", "content": content}],
    }
    # 平台专属参数（如智谱的 thinking 开关）
    payload.update(cfg.get("extra_body") or {})
    return f"{cfg['base_url']}/chat/completions", headers, payload


def _extract_text(cfg: dict, body: dict) -> tuple[str, str]:
    """从两种协议的响应体里取出（模型文本, finish_reason）。"""
    if cfg["protocol"] == "anthropic":
        parts = body.get("content") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return text, str(body.get("stop_reason") or "")
    choices = body.get("choices") or []
    if not choices:
        return "", ""
    first = choices[0]
    finish = str(first.get("finish_reason") or "")
    message = first.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content, finish
    if isinstance(content, list):  # 部分平台按分段返回
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, dict) and p.get("type") in (None, "text")), finish
    return "", finish


def call_vision(images: list[tuple[bytes, str]], system_prompt: str, user_prompt: str,
                max_tokens: int = 1024, timeout: int = 90) -> tuple[dict | None, str]:
    """调用视觉模型并解析 JSON。返回 (结果, 错误说明)，成功时错误说明为空串。

    仅当“拿到了可解析的 JSON”才算成功；其余情况一律给出可行动的原因，
    由调用方决定如何展示，不再吞掉失败细节。
    """
    cfg, err = resolve_config()
    if not cfg:
        return None, err
    if not images:
        return None, "没有可识别的图片内容"

    url, headers, payload = _build_request(cfg, images, system_prompt, user_prompt, max_tokens)

    resp = None
    last_err = ""
    # 限流/服务端抖动值得多等几次：免费视觉模型在高峰时段 429 很常见，
    # 只重试一次会让用户随机遇到“识别失败”，体验上与“功能没做好”无法区分。
    delays = (2.0, 5.0, 8.0)
    attempt = 0
    while True:
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.Timeout:
            last_err = (f"{cfg['label']} 请求超时（{timeout}s）：图片较大或网络较慢，"
                        f"可重试或换更快的模型")
            resp = None
        except requests.RequestException as e:
            return None, f"无法连接 {cfg['label']}（{url}）：{e}"

        if resp is None:
            if attempt < len(delays):
                time.sleep(delays[attempt])
                attempt += 1
                continue
            return None, last_err

        # 鉴权/额度/模型错误重试无意义，只对限流与服务端抖动重试
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < len(delays):
            time.sleep(delays[attempt])
            attempt += 1
            continue
        break

    if resp is None:
        return None, last_err or "请求失败"
    if resp.status_code != 200:
        return None, _friendly_error(resp, cfg)

    try:
        text, finish = _extract_text(cfg, resp.json())
    except Exception as e:
        return None, f"{cfg['label']} 返回内容无法解析：{e}"
    if not text.strip():
        # 思维链占满 token 预算时会返回 finish_reason=length 与空内容，
        # 这种情况必须说清楚，否则会被误判成“图片不清晰”。
        if finish == "length":
            return None, (f"{cfg['label']} 输出被 token 上限截断（模型思考链占用过多）："
                          f"已关闭思考模式仍出现，请改 .env 的 VISION_MODEL 或联系我们")
        return None, f"{cfg['label']} 未返回识别结果（可能被内容策略拦截或图片不清晰）"

    raw = _clean_json(text)
    try:
        parsed = json.loads(raw)
    except Exception:
        # 再试一次完整文本（应对代码块剥离后仍夹带说明文字的情况）
        try:
            parsed = json.loads(text)
        except Exception:
            return None, (f"{cfg['label']} 返回的不是合法 JSON（模型未按格式输出）："
                          f"{raw[:80]}")
    # 一张截图里含多笔回单时，模型会返回 JSON 数组；取第一笔（界面按单笔回单预填）
    if isinstance(parsed, list):
        parsed = next((x for x in parsed if isinstance(x, dict)), None)
    if not isinstance(parsed, dict):
        return None, f"{cfg['label']} 返回的 JSON 结构不是对象，无法提取字段"
    return parsed, ""


def list_models(timeout: int = 30) -> tuple[list[str], str]:
    """列出当前平台可用模型，便于选对 VISION_MODEL。返回 (模型 ID 列表, 错误说明)。"""
    cfg, err = resolve_config()
    if not cfg:
        return [], err
    # 模型列表路径：OpenAI 兼容平台在 base 后接 /models；Anthropic 协议在域名后接 /v1/models
    url = (f"{cfg['base_url']}/v1/models" if cfg["protocol"] == "anthropic"
           else f"{cfg['base_url']}/models")
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {cfg['api_key']}"},
                            timeout=timeout)
    except requests.RequestException as e:
        return [], f"无法连接 {cfg['label']}（{url}）：{e}"
    if resp.status_code != 200:
        return [], _friendly_error(resp, cfg)
    try:
        data = resp.json().get("data") or []
    except Exception as e:
        return [], f"模型列表无法解析：{e}"
    ids = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
    return ids, ""


def diagnose(timeout: int = 30) -> tuple[bool, str]:
    """连通性自检：配置是否完整、key 是否有效、模型是否存在、额度是否可用。

    实现方式是最小成本的文本请求（不带图片），用于把“配置问题”与“识别精度问题”分开。
    返回 (是否可用, 说明)。
    """
    cfg, err = resolve_config()
    if not cfg:
        return False, err

    ids, list_err = list_models(timeout=timeout)
    model_hint = ""
    if ids:
        # 注意：部分平台的 /models 只列文本模型（如智谱不返回视觉模型），
        # 因此“不在列表中”只是弱信号，不能据此判断模型不可用。
        if cfg["model"] in ids:
            model_hint = f"模型 “{cfg['model']}” 在平台模型列表中，存在性正常。"
        else:
            near = [m for m in ids if cfg["model"].split("/")[-1].split("-")[0].lower() in m.lower()]
            model_hint = (f"平台返回 {len(ids)} 个模型，其中未包含当前模型 “{cfg['model']}”；"
                          f"若模型本身可用可忽略此提示（部分平台的模型列表不含视觉模型）。"
                          + (f"相近可选：{'、'.join(near[:5])}。" if near else ""))

    url, headers, payload = _build_request(cfg, [], "只回复 ok", "回复 ok", 16)
    payload["messages"] = [{"role": "user", "content": "ok"}] if cfg["protocol"] == "openai" \
        else [{"role": "user", "content": "ok"}]
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.RequestException as e:
        return False, f"无法连接 {cfg['label']}（{url}）：{e}"

    if resp.status_code == 200:
        return True, (f"{cfg['label']} 配置可用（模型 {cfg['model']}）。{model_hint}").strip()
    reason = _friendly_error(resp, cfg)
    if list_err:
        reason = f"{reason}；模型列表接口也失败：{list_err}"
    return False, reason
