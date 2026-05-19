"""
bili_cookie_refresh.py
B站 Cookie 静默刷新模块

流程：
  检测到 cookie 过期
    → 生成扫码登录二维码
    → 上传图片到飞书（管理员专属频道，不通知团队）
    → 轮询扫码结果（最长等 3 分钟）
    → 扫码成功后自动写回 GitHub Secret
    → 推送"续期成功"通知给管理员

依赖安装：
  pip install qrcode[pil] PyNaCl requests
"""

import base64
import io
import json
import os
import time

import qrcode
import requests

# ─────────────────────────── 常量 ───────────────────────────

BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL     = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

# B站扫码状态码
CODE_SUCCESS      = 0      # 扫码并确认
CODE_NOT_SCANNED  = 86101  # 还没扫
CODE_SCANNED      = 86090  # 已扫，等待确认
CODE_EXPIRED      = 86038  # 二维码已过期


# ─────────────────────────── B站二维码 ───────────────────────────

def generate_bili_qrcode() -> tuple[str, str]:
    """
    调用 B站接口生成登录二维码。
    返回 (qr_url, qrcode_key)
    """
    resp = requests.get(QR_GENERATE_URL, headers=BILI_HEADERS, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data["code"] != 0:
        raise RuntimeError(f"B站生成二维码失败：{data.get('message')}")
    return data["data"]["url"], data["data"]["qrcode_key"]


def qr_url_to_png_bytes(qr_url: str) -> bytes:
    """将二维码链接渲染成 PNG 字节流"""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def poll_scan_result(qrcode_key: str, timeout: int = 180) -> str:
    """
    轮询用户扫码结果，返回新的 cookie 字符串。
    timeout: 最长等待秒数（默认 3 分钟）
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(
            QR_POLL_URL,
            params={"qrcode_key": qrcode_key},
            headers=BILI_HEADERS,
            timeout=10,
        )
        data = resp.json()
        code = data["data"]["code"]

        if code == CODE_SUCCESS:
            # 从 Set-Cookie 里提取完整 cookie
            cookies = dict(resp.cookies)
            if not cookies:
                # 兼容：有时候 cookie 在 data 里
                cookies = {}
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            return cookie_str

        elif code == CODE_EXPIRED:
            raise RuntimeError("二维码已过期，请重新触发流程")

        elif code == CODE_SCANNED:
            print("已扫码，等待 APP 内确认...")

        elif code == CODE_NOT_SCANNED:
            print("等待扫码...")

        time.sleep(3)

    raise TimeoutError(f"超过 {timeout} 秒未完成扫码，流程终止")


# ─────────────────────────── 飞书 ───────────────────────────

def _get_feishu_token(app_id: str, app_secret: str) -> str:
    """获取飞书 tenant_access_token"""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 token 获取失败：{data.get('msg')}")
    return data["tenant_access_token"]


def upload_png_to_feishu(png_bytes: bytes, app_id: str, app_secret: str) -> str:
    """
    将 PNG 上传到飞书，返回 image_key。
    需要飞书自建应用的 app_id / app_secret，
    并在应用权限中开启「发送消息」「上传图片」。
    """
    token = _get_feishu_token(app_id, app_secret)
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/images",
        headers={"Authorization": f"Bearer {token}"},
        data={"image_type": "message"},
        files={"image": ("qrcode.png", png_bytes, "image/png")},
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书图片上传失败：{data.get('msg')}")
    return data["data"]["image_key"]


def send_qrcode_card(image_key: str, admin_webhook: str) -> None:
    """
    向管理员专属 webhook 推送二维码卡片消息。
    使用独立 webhook，团队频道完全不可见。
    """
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🔐 B站登录续期 · 请扫码",
                },
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "游戏情报机器人检测到 B站登录态失效\n"
                            "请用 **B站 APP** 扫描下方二维码重新授权\n\n"
                            "⏰ 有效期：**3 分钟**\n"
                            "✅ 扫码后机器人自动恢复，无需其他操作"
                        ),
                    },
                },
                {
                    "tag": "img",
                    "img_key": image_key,
                    "alt": {"tag": "plain_text", "content": "B站登录二维码"},
                },
            ],
        },
    }
    resp = requests.post(admin_webhook, json=card, timeout=10)
    if resp.status_code != 200 or resp.json().get("StatusCode", 0) != 0:
        raise RuntimeError(f"飞书消息推送失败：{resp.text}")


def send_success_card(admin_webhook: str) -> None:
    """续期成功后通知管理员"""
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "✅ Cookie 续期成功"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "B站登录状态已刷新，游戏情报推送恢复正常运行 🎮\nGitHub Secret 已自动更新，后续无需手动操作。",
                    },
                }
            ],
        },
    }
    requests.post(admin_webhook, json=card, timeout=10)


def send_failure_card(admin_webhook: str, reason: str) -> None:
    """续期失败通知（超时/二维码过期）"""
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "❌ Cookie 续期失败"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"原因：{reason}\n\n今日推送已跳过，请明日自动重试或手动触发 Actions。",
                    },
                }
            ],
        },
    }
    requests.post(admin_webhook, json=card, timeout=10)


# ─────────────────────────── GitHub Secret 更新 ───────────────────────────

def update_github_secret(
    gh_pat: str, owner: str, repo: str, secret_name: str, secret_value: str
) -> bool:
    """
    用 GitHub API 把新 cookie 写回仓库 Secret，免去手动操作。
    gh_pat 需要有 repo 的 secrets 写权限。
    """
    try:
        from nacl import encoding
        from nacl import public as nacl_public
    except ImportError:
        print("⚠️  PyNaCl 未安装，跳过自动更新 Secret（pip install PyNaCl）")
        return False

    headers = {
        "Authorization": f"token {gh_pat}",
        "Accept": "application/vnd.github.v3+json",
    }

    # 1. 获取仓库公钥（用于加密 secret value）
    key_resp = requests.get(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key",
        headers=headers,
        timeout=10,
    )
    key_data = key_resp.json()

    # 2. 用公钥加密 secret value
    pub_key = nacl_public.PublicKey(
        key_data["key"].encode(), encoding.Base64Encoder()
    )
    sealed_box = nacl_public.SealedBox(pub_key)
    encrypted = sealed_box.encrypt(secret_value.encode())
    encrypted_b64 = base64.b64encode(encrypted).decode()

    # 3. PUT 写入
    put_resp = requests.put(
        f"https://api.github.com/repos/{owner}/{repo}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": key_data["key_id"]},
        timeout=10,
    )
    return put_resp.status_code in (201, 204)


# ─────────────────────────── 主入口 ───────────────────────────

def refresh_cookie() -> str | None:
    """
    完整的静默刷新流程。
    成功返回新 cookie 字符串（当前 run 可直接使用）；
    失败返回 None（今日推送跳过，不对团队可见）。
    """
    # 从环境变量读取配置
    app_id        = os.environ["FEISHU_APP_ID"]
    app_secret    = os.environ["FEISHU_APP_SECRET"]
    admin_webhook = os.environ["FEISHU_ADMIN_WEBHOOK"]   # 管理员专属，非团队群
    gh_pat        = os.environ.get("GH_PAT", "")
    repo_full     = os.environ.get("GITHUB_REPOSITORY", "/")  # owner/repo
    owner, repo   = repo_full.split("/", 1)

    try:
        print("🔄 检测到 Cookie 过期，启动静默刷新流程...")

        # 1. 生成二维码
        qr_url, qrcode_key = generate_bili_qrcode()
        png_bytes = qr_url_to_png_bytes(qr_url)

        # 2. 上传图片到飞书
        image_key = upload_png_to_feishu(png_bytes, app_id, app_secret)

        # 3. 推给管理员（仅管理员可见）
        send_qrcode_card(image_key, admin_webhook)
        print("📱 二维码已推送至管理员，等待扫码（最长 3 分钟）...")

        # 4. 等待扫码
        new_cookie = poll_scan_result(qrcode_key, timeout=180)
        print("✅ 扫码成功，获取到新 Cookie")

        # 5. 写回 GitHub Secret（异步不影响当前 run）
        if gh_pat:
            ok = update_github_secret(gh_pat, owner, repo, "BILI_COOKIE", new_cookie)
            print(f"📝 GitHub Secret 更新：{'成功' if ok else '失败（请检查 GH_PAT 权限）'}")

        # 6. 成功通知
        send_success_card(admin_webhook)

        return new_cookie  # 返回给主脚本，本次 run 直接用

    except Exception as e:
        print(f"❌ 刷新流程异常：{e}")
        try:
            send_failure_card(admin_webhook, str(e))
        except Exception:
            pass
        return None  # 主脚本收到 None 后静默跳过，不通知团队
