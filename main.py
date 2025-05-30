#!/usr/bin/env python3
"""
YouTube RSS → Gemini 2.0 Flash → Markdown
毎日 0:00 JST に実行し、直近 24 時間以内に公開された動画だけ処理
"""

import os, re, json, base64, time, pathlib, subprocess, tempfile, datetime, random
import feedparser, requests, yaml
from dotenv import load_dotenv

# ---------- 設定 ----------
load_dotenv()
OUT = pathlib.Path(
    os.getenv("OUTPUT_DIR", "/Users/shee/YOGO/20_library/youtube")
).expanduser()
OUT.mkdir(parents=True, exist_ok=True)

API_KEY = os.getenv("GEMINI_API_KEY")
GEN_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-2.0-flash:generateContent"
)
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
WINDOW_HOURS = 24  # 直近何時間を見るか（デフォルト 24h）

# ---------- 通知 ----------
try:
    from pync import Notifier

    _USE_PYNC = True
except ImportError:
    _USE_PYNC = False


def notify(msg, title="YouTube Bot"):
    if _USE_PYNC:
        try:
            Notifier.notify(msg, title=title)
            return
        except Exception:
            pass
    subprocess.run(
        ["osascript", "-e", f'display notification "{msg}" with title "{title}"']
    )


# ---------- Gemini プロンプト ----------
PROMPT = (
    "1. 日本語で約1000字の要約 (#要約) を作成してください。ebookを翻訳するようにですます調にしてください\n"
    "要約(タグにしないでください)\n"
    "(ここに1000字要約)\n\n"
    "2. 続けて全文を冗長な部分を省きつつ日本語で完全翻訳 (#全文翻訳) してください。\n"
    "全文翻訳（タグにしないでください）\n"
    "(ここに全文翻訳)\n"
)


def gemini_audio(mp3_bytes: bytes) -> str:
    """20 MB 以下なら inline_data、超えたら Files API 経由で呼び出す"""

    # ------------------ ① 20 MB 判定 ------------------
    if len(mp3_bytes) > 20 * 1024 * 1024:  # 20 MB 超ならアップロード
        # -- files:upload --
        up = requests.post(
            UPLOAD_URL,
            params={
                "key": API_KEY,
                "uploadType": "media"
            },
            headers={"Content-Type": "audio/mp3"},
            data=mp3_bytes,
            timeout=300,
        )
        up.raise_for_status()

        j = up.json()
        file_uri = j.get("file", {}).get("uri") or j.get("file", {}).get("name")
        if not file_uri:
            raise RuntimeError(f"upload JSON に name が無い: {up.text}")

        # payload は file_data 参照に変更
        parts = [{"file_data": {"file_uri": file_uri}}, {"text": PROMPT}]
    else:
        # inline_data
        parts = [
            {
                "inline_data": {
                    "mime_type": "audio/mp3",
                    "data": base64.b64encode(mp3_bytes).decode(),
                }
            },
            {"text": PROMPT},
        ]

    payload = {"contents": [{"role": "user", "parts": parts}]}

    # ------------------ ② generateContent ------------------
    for retry in range(5):  # 503/429 用指数バックオフ
        res = requests.post(GEN_URL, params={"key": API_KEY}, json=payload, timeout=300)
        if res.status_code in (429, 503):
            wait = (2**retry) + random.uniform(0, 3)
            notify(f"Gemini {res.status_code} → {wait:.1f}s wait")
            time.sleep(wait)
            continue
        res.raise_for_status()
        # 任意のクールダウン（連続呼び出し抑制）
        time.sleep(random.uniform(2, 5))
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Gemini 503/429 を 5回 リトライしても失敗しました")


# ---------- Shorts 判定 ----------
def is_shorts(url: str) -> bool:
    meta = subprocess.run(
        ["yt-dlp", "-j", "--skip-download", url],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(meta.stdout)
    dur, w, h = data.get("duration", 0), data.get("width", 0), data.get("height", 0)
    return dur <= 60 or (h and w and h > w)


# ---------- 判定関数 ----------
def is_stream(meta: dict) -> bool:
    return (
        meta.get("is_live")
        or meta.get("was_live")
        or meta.get("live_status") in {"is_live", "was_live", "is_upcoming"}
    )


def classify_video(url: str) -> str:
    meta_json = subprocess.run(
        ["yt-dlp", "-j", "--skip-download", url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    data = json.loads(meta_json)

    dur, w, h = data.get("duration", 0), data.get("width", 0), data.get("height", 0)
    if dur <= 60 or (h and w and h > w):
        return "shorts"
    if is_stream(data):
        return "stream"
    return "video"


# ---------- 動画処理 ----------
def handle_entry(entry):
    vid = getattr(entry, "yt_videoid", None)
    if not vid:
        return
    url = f"https://youtu.be/{vid}"
    kind = classify_video(url)
    if kind != "video":
        print(f"   - SKIP {kind} {vid}")
        return

    # ↓ ここから先は従来どおりダウンロード→Gemini へ
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = pathlib.Path(tmp) / f"{vid}.mp3"
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(mp3), url], check=True
        )
        md = gemini_audio(mp3.read_bytes())

    title_safe = re.sub(r'[\\/*?:"<>|]', "", entry.title)[:80]
    channel_safe = re.sub(r'[\\/*?:"<>|]', "", getattr(entry, "author", "unknown"))[:40]
    (OUT / f"{entry.published[:10]}_{title_safe}_{channel_safe}.md").write_text(
        md, encoding="utf-8"
    )
    print(f"   ✔ {title_safe}")
    notify(f"{title_safe} を保存")


# ---------- フィード巡回 ----------
def crawl():
    since_ts = time.time() - WINDOW_HOURS * 3600
    feeds = yaml.safe_load(pathlib.Path("feeds.yaml").read_text()) or []
    for feed_url in feeds:
        if not isinstance(feed_url, str) or not feed_url.strip():
            continue
        print(f"● {feed_url}")
        d = feedparser.parse(feed_url.strip())
        if not d.entries:
            continue
        for e in d.entries:
            if not hasattr(e, "published_parsed"):
                continue
            pub_ts = time.mktime(e.published_parsed)
            if pub_ts < since_ts:  # 24h より古い → スキップ
                continue
            handle_entry(e)
            time.sleep(3)


if __name__ == "__main__":
    crawl()
