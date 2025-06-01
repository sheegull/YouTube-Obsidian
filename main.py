#!/usr/bin/env python3
"""
YouTube RSS → Gemini 2.0 Flash → Markdown
毎日 0:00 JST に実行し、直近 24 時間以内に公開された動画だけ処理
"""

import os, re, json, base64, time, pathlib, subprocess, tempfile, random, calendar
import feedparser, requests, yaml
from datetime import datetime, timezone
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


def notify(msg: str, title: str = "YouTube Bot") -> None:
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
PROMPT_TEMPLATE = (
    """
    あなたは優秀な日英バイリンガル編集者です。以下の指示に従い、YouTube動画の文字起こし全文を処理してください。
    **出力は日本語・Markdown形式、総文字数は必ず3000字以内**に収めてください。絶対に**```を含む行**を出力しないでください。

    =====================
    ### メタデータ

    ---
    必ず最初に動画の詳細データを挿入してください（開始行と終了行を `---` で囲む）。
    実際に取り込んだ動画のデータを以下の形式で記載してください。
    含めるキー:
    - title: {title_ja}
    - original_title: {original_title}
    - url: {url}
    - published: {published}
    ---


    ### 1. 要約 (1000字以内, です/ます調)
    - まず **動画全体を俯瞰した3文のリード文**
    - 次に **キーテーマ** を箇条書き (最大6項目)
    - それぞれのテーマに対応する **主要ポイント** を番号付きリストで記載 (1行70文字以内)
    - 具体的数字・固有名詞を残し、冗長・重複表現は削除
    - 句読点と接続詞を適切に挿入して読みやすく

    ---
    ### 2. 本文簡潔翻訳 (2000字以内, です/ます調)
    - 文字起こし全文を、**冗長な相づち・脱線・繰り返し** を省きながら時系列で翻訳
    - 重要な見出しごとに `####` の小見出しを付け、続けて本文
    - 見出しは`見出し：/n本文`の形式で必ず記載
    - 質問と回答など会話形式は「**Q:**」「**A:**」を用い、読み手が流れを追いやすいように整理
    - 引用・例示・数字・固有名詞は正確に保持

    ---
    ### 3. 次の提案 (任意, 見つかった場合のみ)
    - 動画が提案する **引用記事や文献、論文** や **ツール** があれば箇条書きで列挙
    - 1行150字以内
    - 必ず提案先の論文や記事などのURLを含めること

    =====================
    ### 出力ルールまとめ
    - 全体で**最大3000字**
    - 見出しには `#` をタグとして使わず、必ず `###` から始める
    - 「です/ます」調を徹底
    - 余計な挿入語・口癖・同義反復は削除
    - 指示やコメントは出力しない
    - 指定以外のセクションを追加しない
    """
).lstrip()


# ---------- Gemini 呼び出し ----------
def gemini_audio(mp3_bytes: bytes, prompt: str) -> str:
    """20 MB 以下なら inline_data、超えたら Files API 経由で呼び出す"""
    if len(mp3_bytes) > 20 * 1024 * 1024:
        up = requests.post(
            UPLOAD_URL,
            params={"key": API_KEY, "uploadType": "media"},
            headers={"Content-Type": "audio/mp3"},
            data=mp3_bytes,
            timeout=300,
        )
        up.raise_for_status()
        file_uri = up.json()["file"]["uri"]
        parts = [{"file_data": {"file_uri": file_uri}}, {"text": prompt}]
    else:
        parts = [
            {
                "inline_data": {
                    "mime_type": "audio/mp3",
                    "data": base64.b64encode(mp3_bytes).decode(),
                }
            },
            {"text": prompt},
        ]

    payload = {"contents": [{"role": "user", "parts": parts}]}

    for retry in range(5):
        res = requests.post(GEN_URL, params={"key": API_KEY}, json=payload, timeout=300)
        if res.status_code in (429, 503):
            wait = (2**retry) + random.uniform(0, 3)
            notify(f"Gemini {res.status_code} → {wait:.1f}s wait")
            time.sleep(wait)
            continue
        res.raise_for_status()
        time.sleep(random.uniform(2, 5))
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Gemini 503/429 を 5回 リトライしても失敗しました")


# ---------- Shorts / Stream 判定 ----------
def _yt_meta(url: str) -> dict:
    meta_json = subprocess.run(
        ["yt-dlp", "-j", "--skip-download", url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(meta_json)


def is_shorts(meta: dict) -> bool:
    dur, w, h = meta.get("duration", 0), meta.get("width", 0), meta.get("height", 0)
    return dur <= 60 or (h and w and h > w)


def is_stream(meta: dict) -> bool:
    return (
        meta.get("is_live")
        or meta.get("was_live")
        or meta.get("live_status") in {"is_live", "was_live", "is_upcoming"}
    )


def classify_video(meta: dict) -> str:
    if is_shorts(meta):
        return "shorts"
    if is_stream(meta):
        return "stream"
    return "video"


# ---------- 動画処理 ----------
def build_prompt_from_entry(entry) -> str:
    meta = {
        "title_ja": "",  # 日本語タイトルは Gemini に生成させる
        "original_title": entry.title,
        "url": entry.link,
        "published": entry.published[:10],
    }
    return PROMPT_TEMPLATE.format(**meta)


def sanitize_filename(text: str, max_len: int = 80) -> str:
    return re.sub(r'[\\\\/*?:"<>|]', "", text)[:max_len]


def handle_entry(entry):
    vid = getattr(entry, "yt_videoid", None)
    if not vid:
        return
    url = f"https://youtu.be/{vid}"
    yt_meta = _yt_meta(url)
    if classify_video(yt_meta) != "video":
        print(f"   - SKIP non-video {vid}")
        return

    prompt = build_prompt_from_entry(entry)

    with tempfile.TemporaryDirectory() as tmpdir:
        mp3_path = pathlib.Path(tmpdir) / f"{vid}.mp3"
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(mp3_path), url],
            check=True,
        )
        md = gemini_audio(mp3_path.read_bytes(), prompt)

    fname = f"{entry.published[:10]}_{sanitize_filename(entry.title)}_{sanitize_filename(entry.author,40)}.md"
    (OUT / fname).write_text(md, encoding="utf-8")
    print(f"   ✔ {entry.title}")
    notify(f"{entry.title} を保存")


# ---------- フィード巡回 ----------
def crawl():
    since_ts = time.time() - WINDOW_HOURS * 3600
    feeds = yaml.safe_load(pathlib.Path("feeds.yaml").read_text()) or []
    for feed_url in feeds:
        if not isinstance(feed_url, str) or not feed_url.strip():
            continue
        print(f"● {feed_url}")
        d = feedparser.parse(feed_url.strip())
        for e in d.entries:
            ts_tuple = getattr(e, "published_parsed", None) or getattr(
                e, "updated_parsed", None
            )
            if not ts_tuple:
                continue
            pub_ts = calendar.timegm(ts_tuple)  # UTC epoch
            if pub_ts < since_ts:
                continue
            handle_entry(e)
            time.sleep(3)


if __name__ == "__main__":
    crawl()
