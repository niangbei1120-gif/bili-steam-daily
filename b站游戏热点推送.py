"""
B站 Steam 游戏热点日报（飞书推送）
每天自动抓取 B站 "Steam" 相关视频 → AI 分类 → 推送飞书卡片

使用前请准备：
1. 将 B站登录后的完整 Cookie 粘贴到同目录的 bili_cookie.txt 里（一整行，不用换行）
2. 设置环境变量 DEEPSEEK_API_KEY（你的 DeepSeek API Key）
3. （可选）设置环境变量 FEISHU_WEBHOOK_URL，否则使用代码中的默认值
"""

import os
import json
import time
import hashlib
import urllib.parse
from datetime import datetime

import requests

# ==================== 配置区 ====================

def load_cookie():
    """从同目录下的 bili_cookie.txt 读取 B站 Cookie"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cookie_file = os.path.join(script_dir, "bili_cookie.txt")
    try:
        with open(cookie_file, "r", encoding="utf-8") as f:
            cookie = f.read().strip()
            if not cookie:
                raise ValueError("Cookie 文件为空")
            return cookie
    except Exception as e:
        print(f"❌ 无法读取 {cookie_file}：{e}")
        exit(1)

BILI_COOKIE = load_cookie()
DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
FEISHU_WEBHOOK = os.getenv(
    "FEISHU_WEBHOOK_URL",
    "https://open.feishu.cn/open-apis/bot/v2/hook/6d93a4c3-da65-4f6a-950c-0c100141eb41"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Cookie": BILI_COOKIE,
}
TIMEOUT = 15

# ==================== WBI 签名 ====================

class WbiSigner:
    mixin_key_enc_tab = [
        46,47,18,2,53,8,23,32,15,50,10,31,58,3,45,35,
        27,43,5,49,33,9,42,19,29,28,14,39,12,38,41,13,
        37,48,7,16,24,55,40,61,26,17,0,1,60,51,30,4,
        22,25,54,21,56,59,6,63,57,62,11,36,20,52,44,34
    ]

    def __init__(self):
        self.img_key = None
        self.sub_key = None
        self.last_update = 0

    def get_mixin_key(self, raw_key: str) -> str:
        return "".join([raw_key[i] for i in self.mixin_key_enc_tab])[:32]

    def update_keys(self):
        now = time.time()
        if self.img_key and (now - self.last_update) < 3600:
            return
        resp = requests.get("https://api.bilibili.com/x/web-interface/nav", headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()
        wbi = data["data"]["wbi_img"]
        self.img_key = wbi["img_url"].rsplit("/",1)[1].split(".")[0]
        self.sub_key = wbi["sub_url"].rsplit("/",1)[1].split(".")[0]
        self.last_update = now

    def sign(self, params: dict) -> dict:
        self.update_keys()
        mixin = self.get_mixin_key(self.img_key + self.sub_key)
        params["wts"] = int(time.time())
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        query = urllib.parse.urlencode(sorted_params)
        w_rid = hashlib.md5((query + mixin).encode()).hexdigest()
        params["w_rid"] = w_rid
        return params

signer = WbiSigner()

# ==================== B站搜索 ====================

def search_bilibili(keyword="Steam", page=1, page_size=50):
    """返回按发布时间排序的视频列表"""
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type"
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
        "order": "pubdate",
    }
    signed = signer.sign(params)
    try:
        resp = requests.get(url, params=signed, headers=HEADERS, timeout=TIMEOUT)
        data = resp.json()
        if data["code"] != 0:
            print(f"⚠️ B站搜索错误：{data.get('message', '未知')}")
            return []
        return data.get("data", {}).get("result", [])
    except Exception as e:
        print(f"❌ B站请求失败（第{page}页）：{e}")
        return []

def fetch_recent_videos(max_pages=3):
    """抓取最近Steam相关视频，去重并按播放量降序"""
    all_videos = []
    seen_bvid = set()
    for p in range(1, max_pages + 1):
        items = search_bilibili(page=p)
        if not items:
            break
        for v in items:
            bvid = v.get("bvid", "")
            if bvid in seen_bvid:
                continue
            seen_bvid.add(bvid)
            title = v.get("title", "").replace('<em class="keyword">','').replace('</em>','')
            play = v.get("play", 0)
            pub_ts = v.get("pubdate", 0)
            desc = (v.get("description", "") or "")[:200]
            tags = (v.get("tag", "") or "").split(",")[:5]
            link = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            all_videos.append({
                "title": title,
                "play": play,
                "pub_ts": pub_ts,
                "desc": desc,
                "tags": tags,
                "link": link,
                "bvid": bvid,
            })
        time.sleep(1)
    all_videos.sort(key=lambda x: x["play"], reverse=True)
    return all_videos

# ==================== DeepSeek 分类 ====================

def classify_via_deepseek(videos):
    """用 DeepSeek 对视频列表分类，返回结构化日报（不含链接，只有视频索引）"""
    if not DEEPSEEK_KEY:
        print("⚠️ 未找到 DEEPSEEK_API_KEY，将使用降级模式（直接推送Top5）")
        return None

    candidates = []
    for idx, v in enumerate(videos[:80]):
        candidates.append({
            "id": idx,
            "title": v["title"],
            "play": v["play"],
            "tags": v["tags"],
            "desc": v["desc"],
        })

    system_prompt = f"""你是一个Steam游戏发行团队的舆情分析师。请根据提供的B站视频列表，生成今日的《游戏圈情报日报》。

列表中的每个视频都有一个唯一的编号（id字段）。

要求：
1. 只保留与Steam PC游戏直接相关的内容。以下内容一律丢弃：线下演出、手机游戏、主机独占游戏、行业财经数据、与具体游戏无关的平台政策。
2. 将所有符合条件的视频归入以下四个分类，每个分类最多保留3条：
   🆕 新游速报：Steam近7天内新发售的游戏、刚公布发售日的游戏、刚开放Early Access的新品。
   🔥 热度飙升：播放量高或近期增速快的内容，包括新评测、实况、直播录像等。
   📢 大作动态：知名已发售游戏的大版本更新、DLC、平衡补丁、重大活动。
   📰 圈内大事：Steam平台事件、游戏行业重要事件、发行商动态等（需与Steam游戏相关）。
3. 每条必须包含：
   - game: 游戏名
   - tag: 状态标签（如：今日上线/发售日公布/热度飙升/版本更新等）
   - desc: 一句话描述（≤30字）
   - id: 对应的视频编号（必须从视频列表中选取，不要编造）
4. 最终输出一个JSON数组，格式如下：
[
  {{"category": "新游速报", "items": [{{"game": "...", "tag": "...", "desc": "...", "id": 0}}]}},
  ...
]
只输出JSON，不要包含其他文字。今天是{datetime.now().strftime('%Y-%m-%d')}。
"""

    user_content = json.dumps(candidates, ensure_ascii=False)

    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            timeout=60,
        )
        data = resp.json()
        if "choices" not in data:
            print(f"❌ DeepSeek 返回异常：{data}")
            return None
        raw = data["choices"][0]["message"]["content"].strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]
        categorized = json.loads(raw)
        return categorized
    except Exception as e:
        print(f"❌ DeepSeek 分类失败：{e}")
        return None

# ==================== 飞书卡片消息构建 ====================

def build_feishu_card(categorized, video_map):
    """构建飞书 interactive 卡片消息，含蓝色头部，紧凑排版"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    elements = []

    emoji_map = {
        "新游速报": "🆕",
        "热度飙升": "🔥",
        "大作动态": "📢",
        "圈内大事": "📰",
    }

    for cat in categorized:
        cat_name = cat.get("category", "分类")
        items = cat.get("items", [])
        if not items:
            continue

        # 分类标题
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{emoji_map.get(cat_name, '')} {cat_name}**"
            }
        })

        # 将该分类下所有条目合并为一段长文本（紧凑排版）
        lines = []
        for item in items:
            game = item.get("game", "")
            tag = item.get("tag", "")
            desc = item.get("desc", "")
            vid = item.get("id")
            link = video_map.get(vid, "")

            line = f"**{game}** "
            if tag:
                line += f"`{tag}` "
            line += desc
            if link:
                line += f" [B站]({link})"
            lines.append(line)

        content = "\n".join(lines)    # 用换行连接，内部行间距小

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": content
            }
        })

        # 分类之间的分割线
        elements.append({"tag": "hr"})

    # 底部脚注
    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "每天 09:00 自动推送 · 飞书机器人"
        }
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "游戏圈情报日报"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**{date_str}** · 每日自动推送"
                    }
                },
                {"tag": "hr"},
                *elements
            ]
        }
    }
    return card

def build_fallback_card(videos):
    """降级方案（无 AI Key 时）：卡片消息展示播放量 Top5"""
    top5 = videos[:5]
    elements = []

    for i, v in enumerate(top5, 1):
        title = v["title"]
        play = v["play"]
        link = v["link"]
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{i}. {title}**\n播放量：{play}  [B站]({link})"
            }
        })
        elements.append({"tag": "hr"})

    elements.append({
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": "每天 09:00 自动推送 · 飞书机器人"
        }
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "Steam 热点速递"
                },
                "template": "blue"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "⚠️ 未配置 AI，仅展示播放量 Top5"
                    }
                },
                {"tag": "hr"},
                *elements
            ]
        }
    }
    return card

# ==================== 发送飞书 ====================

def send_feishu(card):
    try:
        r = requests.post(FEISHU_WEBHOOK, json=card, timeout=15)
        if r.status_code == 200:
            print("✅ 飞书推送成功")
        else:
            print(f"❌ 飞书推送失败：{r.status_code} {r.text}")
    except Exception as e:
        print(f"❌ 飞书发送异常：{e}")

# ==================== 主流程 ====================

def main():
    print("📡 正在抓取 B站 Steam 相关视频…")
    videos = fetch_recent_videos(max_pages=3)
    print(f"📊 共获取到 {len(videos)} 条视频")

    categorized = classify_via_deepseek(videos)

    if categorized:
        video_map = {i: v["link"] for i, v in enumerate(videos)}
        card = build_feishu_card(categorized, video_map)
    else:
        card = build_fallback_card(videos)

    print("📨 正在发送飞书卡片…")
    send_feishu(card)
    print("✨ 完成！")

if __name__ == "__main__":
    main()