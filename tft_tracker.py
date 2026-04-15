"""
TFT Tracker v2
情報収集元:
  1. tactics.tools         - Tier list (メタ統計)
  2. Reddit r/CompetitiveTFT - コミュニティの話題投稿・Tip
  3. YouTube RSS           - 最新TFT動画 (.envでチャンネルID設定時のみ)

機能:
  - Set変更を自動検出し、Discordに特別通知 + データリセット
  - 各情報のソースURLをDiscordに表示（タイトルクリックで飛べる）
  - 前回データとの差分でTierの変動を通知
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── 設定 ─────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL = os.getenv("TFT_DISCORD_WEBHOOK_URL")
DATA_FILE = "tft_previous_data.json"
TOP_N = 20  # 表示するコンポジション数

TACTICS_URL = "https://tactics.tools/team-compositions"
REDDIT_URL = "https://www.reddit.com/r/CompetitiveTFT/hot.json"
REDDIT_LINK = "https://www.reddit.com/r/CompetitiveTFT/"

# .env に YOUTUBE_CHANNEL_IDS=UC...,UC... の形式で設定（任意）
_yt_env = os.getenv("YOUTUBE_CHANNEL_IDS", "")
YOUTUBE_CHANNEL_IDS = [cid.strip() for cid in _yt_env.split(",") if cid.strip()]

# Tier 閾値（平均順位）
TIER_THRESHOLDS = {"S": 3.5, "A": 4.0, "B": 4.5}
TIER_EMOJI = {"S": "🏆", "A": "⭐", "B": "🔵", "C": "⬜"}

FIELD_MAX = 1024    # Discord embed フィールドの文字数上限
DESC_MAX = 4096     # Discord embed description の文字数上限


# ── ユーティリティ ────────────────────────────────────────────────

def clean_unit_name(unit_id: str) -> str:
    """'TFT17_Lissandra' → 'Lissandra'"""
    parts = unit_id.split("_")
    return parts[-1] if len(parts) > 1 else unit_id


def get_comp_name(units: list) -> str:
    if not units:
        return "Unknown"
    return " / ".join(clean_unit_name(u) for u in units[:3])


def get_tier(avg_placement: float) -> str:
    if avg_placement < TIER_THRESHOLDS["S"]:
        return "S"
    elif avg_placement < TIER_THRESHOLDS["A"]:
        return "A"
    elif avg_placement < TIER_THRESHOLDS["B"]:
        return "B"
    return "C"


def truncate_lines(lines: list[str], max_len: int = FIELD_MAX) -> str:
    """行リストを max_len 文字以内に収める。"""
    result = []
    total = 0
    for i, line in enumerate(lines):
        needed = len(line) + (1 if result else 0)
        if total + needed > max_len - 20:
            result.append(f"...他 {len(lines) - i} 件")
            break
        result.append(line)
        total += needed
    return "\n".join(result)


# ── データ取得: tactics.tools ─────────────────────────────────────

def fetch_tier_list() -> tuple[list[dict], dict]:
    """
    tactics.tools からtier listを取得する。
    Returns: (comps, set_info)
      set_info = {"set_number": int, "patch": str, "url": str}
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    response = requests.get(TACTICS_URL, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    if not script_tag:
        raise ValueError("ページからデータを取得できません（__NEXT_DATA__ が見つかりません）")

    raw = json.loads(script_tag.string)
    page_props = raw["props"]["pageProps"]

    # Set / Patch 情報を抽出
    # aperture.patch._0 例: 17010 → Set17, Patch 17.1
    aperture = page_props.get("aperture", {})
    patch_raw = aperture.get("patch", {})
    patch_num = patch_raw.get("_0", 0) if isinstance(patch_raw, dict) else 0
    set_number = patch_num // 1000
    minor = (patch_num % 1000) // 10
    patch_str = f"{set_number}.{minor}" if set_number > 0 else "不明"

    set_info = {
        "set_number": set_number,
        "patch": patch_str,
        "url": TACTICS_URL,
    }

    # コンポジション抽出
    groups = page_props["initialData"]["groups"]
    comps = []
    for group in groups:
        for comp in group.get("full", {}).get("comps", []):
            count = int(comp.get("count", 0))
            if count < 100:
                continue

            place = float(comp.get("place", 8.0))
            top4 = int(comp.get("top4", 0))
            win = int(comp.get("win", 0))
            units = comp.get("units", [])
            if isinstance(units, str):
                units = json.loads(units.replace("'", '"'))

            comps.append({
                "name": get_comp_name(units),
                "units": units,
                "tier": get_tier(place),
                "avg_placement": round(place, 2),
                "win_rate": round(win / count * 100, 1),
                "top4_rate": round(top4 / count * 100, 1),
                "count": count,
            })

    comps.sort(key=lambda x: x["avg_placement"])
    return comps[:TOP_N], set_info


# ── データ取得: Reddit ─────────────────────────────────────────────

def fetch_reddit_posts(limit: int = 5) -> list[dict]:
    """r/CompetitiveTFT のホット投稿を取得する。"""
    headers = {"User-Agent": "TFTTracker/2.0 (educational project)"}
    try:
        response = requests.get(
            REDDIT_URL,
            params={"limit": limit * 2},  # フィルタ後に limit 件残るよう多めに取得
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        posts = response.json()["data"]["children"]

        result = []
        for p in posts:
            d = p["data"]
            # 削除済み・自動投稿は除外
            if d.get("removed_by_category") or d.get("author") == "AutoModerator":
                continue
            result.append({
                "title": d["title"],
                "url": f"https://www.reddit.com{d['permalink']}",
                "score": d["score"],
                "flair": d.get("link_flair_text") or "",
                "num_comments": d.get("num_comments", 0),
                "author": d.get("author", ""),
            })
            if len(result) >= limit:
                break

        return result
    except Exception as e:
        print(f"[WARN] Reddit 取得失敗: {e}")
        return []


# ── データ取得: YouTube RSS ───────────────────────────────────────

def fetch_youtube_videos(max_per_channel: int = 3) -> list[dict]:
    """
    YOUTUBE_CHANNEL_IDS に設定されたチャンネルの最新動画を取得する。
    チャンネルIDは .env の YOUTUBE_CHANNEL_IDS に設定（カンマ区切り）。

    チャンネルIDの調べ方:
      YouTubeチャンネルページを開き、右クリック→「ページのソースを表示」で
      'channelId' を検索すると "UC..." から始まるIDが見つかる。
    """
    if not YOUTUBE_CHANNEL_IDS:
        return []

    ATOM_NS = "http://www.w3.org/2005/Atom"
    videos = []

    for channel_id in YOUTUBE_CHANNEL_IDS:
        try:
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            response = requests.get(rss_url, timeout=15)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            ns = {"atom": ATOM_NS}
            channel_name = root.findtext("atom:title", default=channel_id, namespaces=ns) or channel_id

            for entry in root.findall("atom:entry", ns)[:max_per_channel]:
                title = entry.findtext("atom:title", default="", namespaces=ns) or ""
                link_el = entry.find("atom:link", ns)
                url = link_el.get("href", "") if link_el is not None else ""
                published = (entry.findtext("atom:published", default="", namespaces=ns) or "")[:10]

                videos.append({
                    "title": title,
                    "url": url,
                    "channel": channel_name,
                    "published": published,
                })
        except Exception as e:
            print(f"[WARN] YouTube RSS 取得失敗 (channel_id={channel_id}): {e}")

    return videos


# ── データ保存・読み込み ──────────────────────────────────────────

def load_previous_data() -> dict | None:
    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(comps: list[dict], set_info: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "date": datetime.now().isoformat(),
                "set_number": set_info["set_number"],
                "patch": set_info["patch"],
                "comps": comps,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )


# ── 変動検出 ─────────────────────────────────────────────────────

def detect_set_change(current_set: int, previous: dict | None) -> bool:
    """Set番号が変わっていれば True を返す。"""
    if not previous:
        return False
    return previous.get("set_number", 0) != current_set


def detect_tier_changes(current: list[dict], previous: dict | None) -> list[dict]:
    """前回データとのTier変動を返す。"""
    if not previous:
        return []

    prev_map = {c["name"]: c for c in previous.get("comps", [])}
    curr_map = {c["name"]: c for c in current}
    changes = []

    for name, curr in curr_map.items():
        if name in prev_map and curr["tier"] != prev_map[name]["tier"]:
            changes.append({
                "type": "tier_change",
                "name": name,
                "from_tier": prev_map[name]["tier"],
                "to_tier": curr["tier"],
                "avg_placement": curr["avg_placement"],
            })

    for name in set(curr_map) - set(prev_map):
        changes.append({"type": "new", "name": name, "tier": curr_map[name]["tier"],
                        "avg_placement": curr_map[name]["avg_placement"]})

    for name in set(prev_map) - set(curr_map):
        changes.append({"type": "dropped", "name": name, "tier": prev_map[name]["tier"]})

    return changes


# ── Discord Embed 構築 ────────────────────────────────────────────

def build_new_set_embed(set_info: dict, prev_set: int) -> dict:
    """Set変更アラートのEmbedを構築する。"""
    return {
        "title": f"🆕 TFT Set {set_info['set_number']} 開始！",
        "url": set_info["url"],
        "description": (
            f"新しいSetが始まりました！\n"
            f"**Set {prev_set}** → **Set {set_info['set_number']} (Patch {set_info['patch']})**\n\n"
            f"前回のデータをリセットし、新しいTier listを取得します。"
        ),
        "color": 0xFFD700,
    }


def build_tier_list_embed(comps: list[dict], changes: list[dict], set_info: dict) -> dict:
    """Tier list EmbedをSet情報付きで構築する。タイトルがURL（クリック可能）。"""
    tier_groups: dict[str, list] = {"S": [], "A": [], "B": [], "C": []}
    for comp in comps:
        tier_groups[comp["tier"]].append(comp)

    fields = []
    for tier in ["S", "A", "B", "C"]:
        items = tier_groups[tier]
        if not items:
            continue
        lines = [
            f"**{c['name']}** — {c['avg_placement']}位 | 勝率{c['win_rate']}%"
            for c in items
        ]
        fields.append({
            "name": f"{TIER_EMOJI[tier]} {tier}ティア ({len(items)}件)",
            "value": truncate_lines(lines),
            "inline": False,
        })

    # 変動フィールド
    if changes:
        change_lines = []
        for ch in changes:
            if ch["type"] == "tier_change":
                arrow = "⬆️" if ch["to_tier"] < ch["from_tier"] else "⬇️"
                change_lines.append(
                    f"{arrow} **{ch['name']}**: {ch['from_tier']} → {ch['to_tier']} ({ch['avg_placement']}位)"
                )
            elif ch["type"] == "new":
                change_lines.append(
                    f"🆕 **{ch['name']}** がTop{TOP_N}に登場 ({ch['tier']}ティア)"
                )
            elif ch["type"] == "dropped":
                change_lines.append(f"❌ **{ch['name']}** が圏外に")
        if change_lines:
            fields.append({
                "name": "📊 前回からの変動",
                "value": truncate_lines(change_lines),
                "inline": False,
            })

    return {
        "title": f"🎮 TFT Set {set_info['set_number']} Tier List (Patch {set_info['patch']})",
        "url": set_info["url"],   # タイトルをクリックで tactics.tools へ
        "description": f"更新日時: {datetime.now().strftime('%Y/%m/%d %H:%M')} | 上位{TOP_N}コンポジション",
        "color": 0x1E90FF,
        "fields": fields,
        "footer": {"text": "Source: tactics.tools — タイトルクリックで詳細へ"},
    }


def build_reddit_embed(posts: list[dict]) -> dict | None:
    """Reddit投稿EmbedをURL付きで構築する。"""
    if not posts:
        return None

    lines = []
    for p in posts:
        title_short = p["title"][:60] + ("..." if len(p["title"]) > 60 else "")
        flair_str = f" `{p['flair']}`" if p["flair"] else ""
        lines.append(
            f"[{title_short}]({p['url']}){flair_str}\n"
            f"　↑{p['score']} | 💬{p['num_comments']} | by u/{p['author']}"
        )

    return {
        "title": "📰 r/CompetitiveTFT 話題の投稿",
        "url": REDDIT_LINK,   # タイトルクリックでサブレへ
        "description": truncate_lines(lines, DESC_MAX),
        "color": 0xFF4500,
        "footer": {"text": "Source: Reddit r/CompetitiveTFT — タイトルクリックでサブレへ"},
    }


def build_youtube_embed(videos: list[dict]) -> dict | None:
    """YouTube動画EmbedをURL付きで構築する。"""
    if not videos:
        return None

    lines = []
    for v in videos:
        title_short = v["title"][:55] + ("..." if len(v["title"]) > 55 else "")
        lines.append(f"[{title_short}]({v['url']})\n　{v['channel']} | {v['published']}")

    return {
        "title": "▶️ 最新 TFT 動画",
        "url": "https://www.youtube.com/results?search_query=Teamfight+Tactics+TFT",
        "description": truncate_lines(lines, DESC_MAX),
        "color": 0xFF0000,
        "footer": {"text": "Source: YouTube RSS — タイトルクリックでYouTube検索へ"},
    }


# ── Discord 送信 ──────────────────────────────────────────────────

def send_discord_notification(
    comps: list[dict],
    changes: list[dict],
    set_info: dict,
    reddit_posts: list[dict],
    youtube_videos: list[dict],
    is_new_set: bool,
    prev_set: int = 0,
) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] TFT_DISCORD_WEBHOOK_URL が .env に設定されていません")
        return

    embeds = []

    # Set変更アラート（最初に表示）
    if is_new_set:
        embeds.append(build_new_set_embed(set_info, prev_set))

    # Tier list
    embeds.append(build_tier_list_embed(comps, changes, set_info))

    # Reddit
    reddit_embed = build_reddit_embed(reddit_posts)
    if reddit_embed:
        embeds.append(reddit_embed)

    # YouTube
    yt_embed = build_youtube_embed(youtube_videos)
    if yt_embed:
        embeds.append(yt_embed)

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": embeds},
        timeout=15,
    )
    if not response.ok:
        print(f"[ERROR] Discord 送信失敗 ({response.status_code}): {response.text}")
        response.raise_for_status()
    print("[OK] Discord に通知を送信しました")


# ── メイン ────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now().strftime('%Y/%m/%d %H:%M')}] TFT 情報を収集中...")

    # 各ソースからデータ取得
    comps, set_info = fetch_tier_list()
    print(f"  -> Tier list: {len(comps)} 件 (Set {set_info['set_number']}, Patch {set_info['patch']})")

    reddit_posts = fetch_reddit_posts(limit=5)
    print(f"  -> Reddit: {len(reddit_posts)} 件")

    youtube_videos = fetch_youtube_videos(max_per_channel=3)
    print(f"  -> YouTube: {len(youtube_videos)} 件")

    # 前回データと比較
    previous = load_previous_data()
    is_new_set = detect_set_change(set_info["set_number"], previous)
    prev_set = previous.get("set_number", 0) if previous else 0

    # Set変更時は変動比較をリセット（別Setのデータと比較しても意味がない）
    changes = [] if is_new_set else detect_tier_changes(comps, previous)

    if is_new_set:
        print(f"  -> Set変更を検出！ Set {prev_set} → Set {set_info['set_number']}")
    else:
        print(f"  -> 変動: {len(changes)} 件")

    # Discord 通知
    send_discord_notification(comps, changes, set_info, reddit_posts, youtube_videos, is_new_set, prev_set)

    # データ保存
    save_data(comps, set_info)
    print("[OK] 完了")


if __name__ == "__main__":
    main()
