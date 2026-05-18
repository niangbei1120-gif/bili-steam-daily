"""
B站 Steam 游戏热点日报（飞书推送 + GitHub Pages HTML）
每天自动抓取 B站 "Steam" 相关视频 + RSS 游戏媒体 → AI 分类 → 推送飞书卡片 + 生成 HTML 日报

使用前请准备：
1. 将 B站登录后的完整 Cookie 粘贴到同目录的 bili_cookie.txt 里（一整行，不用换行）
2. 设置环境变量 DEEPSEEK_API_KEY（你的 DeepSeek API Key）
3. （可选）设置环境变量 FEISHU_WEBHOOK_URL，否则使用代码中的默认值
4. 环境变量 GITHUB_REPOSITORY 由 GitHub Actions 自动注入（owner/repo 格式），用于生成 Pages 链接
"""

import os
import json
import time
import hashlib
import urllib.parse
import re
from datetime import datetime

import requests
import feedparser

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
                "source": "B站",
            })
        time.sleep(1)
    all_videos.sort(key=lambda x: x["play"], reverse=True)
    return all_videos

def fetch_rss_news():
    """抓取所有配置的 RSS 源，返回统一格式的新闻列表"""
    rss_feeds = [
        ("机核", "https://www.gcores.com/rss"),
        ("游民星空", "https://www.gamersky.com/rss/news"),
        ("3DM", "https://www.3dmgame.com/rss/news"),
        ("其乐Keylol", "https://keylol.com/forum.php?mod=rss"),
        ("indienova", "https://indienova.com/feed"),
        ("游戏葡萄", "https://youxiputao.com/feed"),
    ]
    
    all_news = []
    print(f"📡 正在抓取 {len(rss_feeds)} 个 RSS 源…")
    
    for source_name, url in rss_feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "")
                pub_ts = time.mktime(entry.published_parsed) if entry.get("published_parsed") else 0
                desc = (entry.get("summary", "") or entry.get("description", ""))[:200]
                desc = re.sub(r'<[^>]+>', '', desc).strip()

                all_news.append({
                    "title": title,
                    "play": 0,
                    "pub_ts": pub_ts,
                    "desc": desc,
                    "tags": [source_name],
                    "link": link,
                    "bvid": "",
                    "source": source_name,
                })
        except Exception as e:
            print(f"❌ RSS抓取失败 [{source_name}]：{e}")
            
    all_news.sort(key=lambda x: x["pub_ts"], reverse=True)
    print(f"✅ RSS抓取完成，共获取 {len(all_news)} 条新闻")
    return all_news

# ==================== DeepSeek 分类 ====================

def classify_via_deepseek(videos):
    if not DEEPSEEK_KEY:
        return None, None

    bili_items = [v for v in videos if v.get("source") == "B站"][:50]
    rss_items  = [v for v in videos if v.get("source") != "B站"][:60]
    selected   = bili_items + rss_items

    candidates = []
    for idx, v in enumerate(selected):
        candidates.append({
            "id": idx,
            "title": v["title"],
            "play": v["play"],
            "tags": v["tags"],
            "desc": v["desc"],
            "source": v.get("source", "未知"),
        })

    system_prompt = f"""你是一个专业的游戏行业舆情分析师。请根据提供的多平台内容列表（含B站视频与各游戏媒体文章），生成一份《游戏圈情报日报》。

列表中的每个内容都有一个唯一的编号（id字段），以及其来源平台（source字段）。

**要求：**
1.  **严苛筛选**：只保留与 PC / Steam 游戏直接相关的内容，**果断丢弃**：纯手游、主机独占游戏、线下活动、与游戏无关的财经/社会新闻、泛科技新闻。同一款游戏的同一事件只保留描述最完整的一条，禁止重复入选。
2.  **来源均衡**：所有平台（B站、机核、游民星空、3DM、其乐Keylol、indienova、游戏葡萄）地位平等，不能只选B站。尽量从多个来源选取最优质的内容，避免单一平台垄断。
3.  **特别过滤**：其乐Keylol 来源中，纯打折促销、史低特卖、喜加一领取类内容一律丢弃，只保留有实质内容的讨论帖或新闻。
4.  **精准分类**：将所有符合条件的内容归入以下四类，按顺序输出，优先级依次降低。
    *   **新游速报**：Steam新游上线、测试、公布发售日、重大版本更新。最多3条。
    *   **热度飙升**：在玩家社群中正在快速传播的热门事件、出圈话题、争议、名场面、热梗。最多3条。
    *   **圈内大事**：对游戏行业有影响的产业新闻、发行商重大决策、核心玩家社区事件。最多3条。
    *   **平台活动**：Steam游戏节/新品节、Epic喜加一、大型促销活动（非史低打折类）。最多2条，放在最后。
5.  **输出格式**：
    *   输出一个 JSON 数组，每个元素代表一个分类，格式如下：
    *   `[{{"category": "新游速报", "items": [{{"game": "游戏名", "tag": "状态标签", "desc": "一句话描述", "id": 对应编号}}, ...]}}]`
    *   category 只能从上述四个分类名（新游速报、热度飙升、圈内大事、平台活动）中选取，items 中每条必须包含 game、tag、desc、id 四个字段。
    *   只输出 JSON，不要任何额外文字或 Markdown 代码块包裹。

今天是{datetime.now().strftime('%Y-%m-%d')}。
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
            return None, None

        raw = data["choices"][0]["message"]["content"].strip()
        print("========== AI 原始返回 ==========")
        print(raw)
        print("==================================")

        # 清洗 Markdown 代码块包裹
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[:-3]

        categorized = json.loads(raw)
        return categorized, selected
    except Exception as e:
        print(f"❌ DeepSeek 分类失败：{e}")
        return None, None

# ==================== HTML 日报生成 ====================

def generate_html_report(categorized, video_map, date_str):
    """生成美化版 HTML 日报，保存到 report/{date_str}.html"""

    section_cfg = {
        "新游速报":  {"emoji": "🆕", "hbg": "#dbeafe", "hcolor": "#1e40af"},
        "热度飙升":  {"emoji": "🔥", "hbg": "#fee2e2", "hcolor": "#991b1b"},
        "圈内大事":  {"emoji": "📰", "hbg": "#f1f5f9", "hcolor": "#334155"},
        "平台活动":  {"emoji": "📢", "hbg": "#dcfce7", "hcolor": "#166534"},
        "今日游戏热点": {"emoji": "🎮", "hbg": "#ede9fe", "hcolor": "#4c1d95"},
    }

    # 兼容两种格式
    if isinstance(categorized, list) and len(categorized) > 0:
        if "category" in categorized[0]:
            categories = categorized
        else:
            categories = [{"category": "今日游戏热点", "items": categorized}]
    else:
        categories = []

    total_items = sum(len(c.get("items", [])) for c in categories)

    sections_html = ""
    for cat in categories:
        cat_name = cat.get("category", "今日游戏热点")
        items = cat.get("items", [])
        if not items:
            continue

        cfg = section_cfg.get(cat_name, section_cfg["今日游戏热点"])
        emoji = cfg["emoji"]
        hbg = cfg["hbg"]
        hcolor = cfg["hcolor"]

        items_html = ""
        for item in items:
            game = item.get("game", "")
            tag = item.get("tag", "")
            desc = item.get("desc", "")
            vid = item.get("id")
            info = video_map.get(vid, {})
            link = info.get("link", "")
            source = info.get("source", "未知")

            tag_html = f'<span class="tag">{tag}</span>' if tag else ""
            link_html = (
                f'<a href="{link}" target="_blank" rel="noopener" class="link-arrow">↗</a>'
                if link else ""
            )

            items_html += f"""
        <div class="item">
          <div class="item-body">
            <div class="item-top">
              <span class="source">{source}</span>
              <span class="game">{game}</span>
              {tag_html}
            </div>
            <p class="desc">{desc}</p>
          </div>
          {link_html}
        </div>"""

        sections_html += f"""
      <div class="section">
        <div class="section-head" style="background:{hbg}">
          <span class="section-emoji">{emoji}</span>
          <span class="section-name" style="color:{hcolor}">{cat_name}</span>
          <span class="section-count">{len(items)} 条</span>
        </div>
        <div class="section-body">{items_html}
        </div>
      </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>游戏圈情报日报 · {date_str}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f8fafc;
    color: #1e293b;
    padding: 24px 16px 48px;
    line-height: 1.6;
  }}
  .container {{ max-width: 720px; margin: 0 auto; }}

  /* Header */
  .header {{ margin-bottom: 24px; }}
  .header-top {{ display: flex; align-items: center; gap: 10px; margin-bottom: 4px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; background: #3b82f6; flex-shrink: 0; }}
  .title {{ font-size: 20px; font-weight: 600; color: #0f172a; }}
  .subtitle {{ font-size: 13px; color: #64748b; margin-left: 18px; }}

  /* Stats */
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 24px; }}
  .stat {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 12px 14px; }}
  .stat-label {{ font-size: 12px; color: #94a3b8; margin-bottom: 4px; }}
  .stat-num {{ font-size: 22px; font-weight: 600; color: #0f172a; }}

  /* Sections */
  .sections {{ display: flex; flex-direction: column; gap: 16px; }}
  .section {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; overflow: hidden; }}
  .section-head {{
    display: flex; align-items: center; gap: 8px;
    padding: 10px 16px;
    border-bottom: 1px solid #e2e8f0;
  }}
  .section-emoji {{ font-size: 15px; }}
  .section-name {{ font-size: 14px; font-weight: 600; flex: 1; }}
  .section-count {{
    font-size: 11px; color: #94a3b8;
    background: #f1f5f9; padding: 2px 8px;
    border-radius: 20px;
  }}
  .section-body {{ padding: 4px 0; }}

  /* Items */
  .item {{
    display: flex; align-items: flex-start; gap: 10px;
    padding: 12px 16px;
    border-bottom: 1px solid #f1f5f9;
  }}
  .item:last-child {{ border-bottom: none; }}
  .item-body {{ flex: 1; min-width: 0; }}
  .item-top {{
    display: flex; align-items: center; gap: 6px;
    flex-wrap: wrap; margin-bottom: 4px;
  }}
  .source {{
    font-size: 11px; font-weight: 500;
    background: #eff6ff; color: #2563eb;
    padding: 1px 6px; border-radius: 4px;
    flex-shrink: 0;
  }}
  .game {{ font-size: 14px; font-weight: 600; color: #0f172a; }}
  .tag {{
    font-size: 11px; color: #475569;
    background: #f1f5f9; border: 1px solid #e2e8f0;
    padding: 1px 7px; border-radius: 4px;
    font-family: monospace;
  }}
  .desc {{ font-size: 13px; color: #475569; line-height: 1.55; }}
  .link-arrow {{
    font-size: 16px; color: #94a3b8;
    text-decoration: none; flex-shrink: 0;
    margin-top: 2px; transition: color 0.15s;
  }}
  .link-arrow:hover {{ color: #3b82f6; }}

  /* Footer */
  .footer {{
    margin-top: 32px; text-align: center;
    font-size: 12px; color: #94a3b8;
  }}

  @media (max-width: 480px) {{
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .title {{ font-size: 17px; }}
  }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div class="header-top">
      <div class="dot"></div>
      <span class="title">游戏圈情报日报</span>
    </div>
    <div class="subtitle">{date_str} · 自动抓取 B站 + 6 路 RSS · DeepSeek AI 分类</div>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-label">精选条目</div>
      <div class="stat-num">{total_items}</div>
    </div>
    <div class="stat">
      <div class="stat-label">内容板块</div>
      <div class="stat-num">{len(categories)}</div>
    </div>
    <div class="stat">
      <div class="stat-label">来源平台</div>
      <div class="stat-num">7</div>
    </div>
    <div class="stat">
      <div class="stat-label">推送时间</div>
      <div class="stat-num" style="font-size:16px">09:00</div>
    </div>
  </div>

  <div class="sections">
    {sections_html}
  </div>

  <div class="footer">
    每天 09:00 自动推送 · 由 GitHub Actions 生成 · 飞书机器人
  </div>

</div>
</body>
</html>"""

    os.makedirs("report", exist_ok=True)
    output_path = os.path.join("report", f"{date_str}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ HTML 日报已生成：{output_path}")
    return output_path


def get_pages_url(date_str):
    """根据 GITHUB_REPOSITORY 环境变量构造 GitHub Pages 链接"""
    repo = os.getenv("GITHUB_REPOSITORY", "")
    if not repo or "/" not in repo:
        return ""
    owner, repo_name = repo.split("/", 1)
    return f"https://{owner}.github.io/{repo_name}/report/{date_str}.html"


# ==================== 飞书卡片消息构建 ====================

def build_feishu_card(categorized, video_map, report_url=""):
    """构建飞书 interactive 卡片消息"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    elements = []

    emoji_map = {
        "新游速报":    "🆕",
        "热度飙升":    "🔥",
        "圈内大事":    "📰",
        "平台活动":    "📢",
        "今日游戏热点": "🎮",
    }

    # 兼容两种数据格式
    if isinstance(categorized, list) and len(categorized) > 0:
        if "category" in categorized[0]:
            categories = categorized
        else:
            categories = [{"category": "今日游戏热点", "items": categorized}]
    else:
        categories = []

    for cat in categories:
        cat_name = cat.get("category", "今日游戏热点")
        items = cat.get("items", [])
        if not items:
            continue

        emoji = emoji_map.get(cat_name, "")
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{emoji} {cat_name}**"
            }
        })

        lines = []
        for item in items:
            game = item.get("game", "")
            tag = item.get("tag", "")
            desc = item.get("desc", "")
            vid = item.get("id")
            info = video_map.get(vid, {})
            link = info.get("link", "")
            source = info.get("source", "未知来源")

            line = f"【{source}】**{game}**"
            if tag:
                line += f" `{tag}`"
            line += f" {desc}"
            if link:
                line += f" [🔗]({link})"
            lines.append(line)

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(lines)
            }
        })
        elements.append({"tag": "hr"})

    # 底部脚注
    footer_text = "每日自动推送 · 飞书机器人"
    if report_url:
        footer_text += f"　　[📄 查看完整日报]({report_url})"
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": footer_text}]
    })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"游戏圈情报日报 · {date_str}"
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

# ==================== 兜底卡片 ====================

def build_fallback_card(videos):
    """DeepSeek 失败时发送播放量前10的视频列表"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    lines = []
    for v in videos[:10]:
        title = v.get("title", "")
        link = v.get("link", "")
        play = v.get("play", 0)
        line = f"**{title}** 播放 {play:,}"
        if link:
            line += f" [🔗]({link})"
        lines.append(line)

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"Steam 热门视频 · {date_str}"},
                "template": "yellow"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "\n".join(lines)}
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "⚠️ AI 分类失败，展示播放量 Top 10"}]
                }
            ]
        }
    }

# ==================== 发送飞书 ====================

def send_feishu(card):
    try:
        r = requests.post(FEISHU_WEBHOOK, json=card, timeout=15)
        if r.status_code == 200:
            result = r.json()
            print(f"飞书响应：{result}")
            if result.get("code") == 0:
                print("✅ 飞书推送成功")
            else:
                print(f"❌ 飞书推送失败：{result.get('msg')}")
        else:
            print(f"❌ HTTP错误：{r.status_code}")
    except Exception as e:
        print(f"❌ 飞书发送异常：{e}")

def send_feishu_alert(text):
    """发送纯文本警报到飞书"""
    try:
        r = requests.post(FEISHU_WEBHOOK, json={"msg_type": "text", "content": {"text": text}}, timeout=15)
        if r.status_code == 200:
            print(f"✅ 飞书警报已发送：{text}")
    except Exception as e:
        print(f"❌ 飞书警报发送失败：{e}")

# ==================== 主流程 ====================

def main():
    date_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 抓取多路数据源
    print("📡 正在抓取 B站 Steam 相关视频…")
    videos = fetch_recent_videos(max_pages=3)
    print(f"📺 B站视频: {len(videos)} 条")
    if not videos:
        send_feishu_alert("⚠️ B站 Cookie 可能已过期，今日视频抓取 0 条，请前往仓库 Settings → Secrets 更新 BILI_COOKIE")

    rss_items = fetch_rss_news()

    # 2. 合并所有内容
    all_items = videos + rss_items
    print(f"📊 总信息源: {len(all_items)} 条")

    if not all_items:
        print("❌ 无任何内容，退出")
        return

    # 3. AI 统一分类
    categorized, selected = classify_via_deepseek(all_items)

    video_map = {
        i: {
            "link": v.get("link", ""),
            "source": v.get("source", "未知")
        }
        for i, v in enumerate(selected or [])
    }

    # 4. 生成 HTML 日报
    report_url = ""
    if categorized:
        generate_html_report(categorized, video_map, date_str)
        report_url = get_pages_url(date_str)
        if report_url:
            print(f"🌐 日报链接：{report_url}")

    # 5. 构建并发送飞书卡片
    if categorized:
        card = build_feishu_card(categorized, video_map, report_url=report_url)
    else:
        card = build_fallback_card(videos)

    print("📨 正在发送飞书卡片…")
    send_feishu(card)
    print("✨ 完成！")

if __name__ == "__main__":
    main()
