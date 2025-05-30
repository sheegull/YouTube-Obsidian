#!/usr/bin/env python3
"""
YouTube RSS → Gemini 2.0 Flash → Markdown
macOS だけで通知 (Slack 無し)
"""

import os, json, base64, time, pathlib, datetime, subprocess, tempfile, shutil
import feedparser, requests, yaml
from dotenv import load_dotenv
from typing import Dict

load_dotenv()
OUT = pathlib.Path(os.getenv("OUTPUT_DIR", "/Users/shee/YOGO/20_library/youtube"))
OUT.mkdir(parents=True, exist_ok=True)

# ---- macOS 通知ユーティリティ ---------------------------------------------
try:
    from pync import Notifier       # terminal-notifier ラッパー :contentReference[oaicite:2]{index=2}
    _USE_PYNC = True
except ImportError:
    _USE_PYNC = False               # fallback: osascript

def notify(msg: str, title: str = "YouTube Bot") -> None:
    """macOS 通知 (pync→osascript の順で試す）"""
    if _USE_PYNC:
        try:
            Notifier.notify(msg, title=title)   # :contentReference[oaicite:3]{index=3}
            return
        except Exception:
            pass
    subprocess.run(
        ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
        check=False,
    )                                          # :contentReference[oaicite:4]{index=4}

# ---- キャッシュファイル ------------------------------------------------------
CACHE = pathlib.Path("processed.json")
processed: Dict[str, str] = {}
if CACHE.exists():
    try:
        processed = json.loads(CACHE.read_text())
    except json.JSONDecodeError:
        notify("processed.json が壊れています。初期化します")
        processed = {}

# ---- Gemini 2.0 Flash -------------------------------------------------------
API_KEY = os.getenv("GEMINI_API_KEY")
GEN_URL  = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

def gemini_audio(audio_bytes: bytes) -> str:
    """音声→文字起こし→1000字要約→全文和訳"""
    payload = {
        "contents": [{
            "role": "user",
            "parts":[
                {"inline_data":{
                    "mime_type": "audio/mp3",
                    "data": base64.b64encode(audio_bytes).decode()
                }},
                {"text": (
                    "以下の音声を書き起こし、1000字で全体要約し、"
                    "続けて全文をわかりやすい日本語に翻訳してください。"
                    "出力フォーマット:\n#要約\n...\n#全文翻訳\n..."
                )}
            ]
        }]
    }

    for attempt in range(5):
        res = requests.post(GEN_URL, params={"key": API_KEY}, json=payload, timeout=120)
        if res.status_code == 429:
            notify("⚠️ Gemini 429: 60 秒後にリトライ")
            time.sleep(60)
            continue
        res.raise_for_status()
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Gemini 429 を5回リトライしましたが復帰できません")

# ---- 動画 1 本を処理 ---------------------------------------------------------
def handle_entry(entry) -> None:
    vid = getattr(entry, "yt_videoid", None)
    if not vid or vid in processed:
        return

    url = f"https://youtu.be/{vid}"
    with tempfile.TemporaryDirectory() as tmp:
        # ---- 音声だけダウンロード（yt-dlp）-----------------------------------
        target = pathlib.Path(tmp) / f"{vid}.mp3"
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(target), url],
            check=True, capture_output=True
        )                                       # yt-dlp 公式 :contentReference[oaicite:5]{index=5}

        text_md = gemini_audio(target.read_bytes())
        out_file = OUT / f"{entry.published[:10]}_{vid}.md"
        out_file.write_text(text_md, encoding="utf-8")

    processed[vid] = entry.published
    CACHE.write_text(json.dumps(processed, ensure_ascii=False, indent=2))
    notify(f"✅ {entry.title} を保存しました")

# ---- RSS 巡回 ----------------------------------------------------------------
def crawl() -> None:
    feeds = yaml.safe_load(pathlib.Path("feeds.yaml").read_text())  # :contentReference[oaicite:6]{index=6}
    for feed in feeds:
        try:
            for entry in feedparser.parse(feed).entries:            # :contentReference[oaicite:7]{index=7}
                handle_entry(entry)
        except Exception as exc:
            notify(f"❌ フィード処理失敗: {feed} | {exc}")

if __name__ == "__main__":
    crawl()
