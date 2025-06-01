#!/usr/bin/env python3
"""
YouTube RSS → Gemini 2.0 Flash → Markdown
Asia/Tokyoで毎日 0:00 に実行し、WINDOW_HOURS 内の新着のみ処理
"""

import os, re, json, base64, time, pathlib, subprocess, tempfile, random, calendar
import feedparser, requests, yaml
from datetime import datetime, timezone, UTC
from dotenv import load_dotenv

# ---------- 設定 ----------
load_dotenv()
OUT_YT = pathlib.Path(os.getenv("OUTPUT_DIR_YT", "/Users/shee/YOGO/20_library/youtube"))
OUT_POD = pathlib.Path(
    os.getenv("OUTPUT_DIR_POD", "/Users/shee/YOGO/20_library/podcast")
)
for p in (OUT_YT, OUT_POD):
    p.expanduser().mkdir(parents=True, exist_ok=True)

API_KEY = os.getenv("GEMINI_API_KEY")
GEN_URL = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-2.0-flash:generateContent"
)
UPLOAD_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
WINDOW_HOURS = 24  # 直近何時間を見るか

# ---------- 通知 ----------
try:
    from pync import Notifier

    _USE_PYNC = True
except ImportError:
    _USE_PYNC = False


def notify(msg: str, title: str = "YouTube & Podcast Bot") -> None:
    if _USE_PYNC:
        try:
            Notifier.notify(msg, title=title)
            return
        except Exception:
            pass

    try:
        import subprocess, shlex

        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"']
        )
    except Exception:
        print(f"[NOTIFY] {title}: {msg}")


# ========== ユーティリティ ==========
def sanitize_filename(text: str, max_len: int = 80) -> str:
    """ファイル名に使えない文字を削除 & 長さ制限"""
    return re.sub(r'[\\/*?:"<>|]', "", text)[:max_len]


# ---------- Gemini プロンプト ----------
PROMPT_TMPL = (
    """
    あなたは優秀な日英バイリンガル編集者です。以下の指示に従い、コンテンツの文字起こし全文を処理してください。
    **出力は日本語・Markdown形式、総文字数は必ず3000字以内**に収めてください。コードブロックは禁止です。

    =====================
    ### メタデータ

    ---
    ### YAMLメタデータ
    必ず最初に YAML フロントマターを挿入してください（開始行と終了行を --- で囲む）。
    実際に取り込んだ動画/音声のデータを以下の形式で記載してください。
    含めるキー:
    - title: {title_ja}
    - original_title: {original_title}
    - url: {url}
    - published: {published}
    ---


    ### 1. 要約 (1000字以内, です/ます調)
    - まず **動画/音声全体を俯瞰した3文のリード文**
    - 次に **キーテーマ** を箇条書き (最大6項目)
    - それぞれのテーマに対応する **主要ポイント** を番号付きリストで記載 (1行70文字以内)
    - 具体的数字・固有名詞を残し、冗長・重複表現は削除
    - 句読点と接続詞を適切に挿入して読みやすく

    ---
    ### 2. ポイント (2000字以内, です/ます調)
    - 文字起こし全文を、**冗長な相づち・脱線・繰り返し** を省きながら時系列で翻訳
    - 重要な見出しごとに `####` の小見出しを付け、続けて本文
    - 見出しは`見出し：/n本文`の形式で必ず記載
    - 質問と回答など会話形式は「**Q:**」「**A:**」を用い、読み手が流れを追いやすいように整理
    - 引用・例示・数字・固有名詞は正確に保持

    ---
    ### 3. 次の提案 (任意, 見つかった場合のみ)
    - 引用記事・文献・論文やツールがあれば紹介、提案されていれば箇条書きで列挙
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


def build_prompt(entry) -> str:
    url = (
        getattr(entry, "link", None)
        or getattr(entry, "id", "")
        or (entry.enclosures[0].href if getattr(entry, "enclosures", []) else "")
    )

    meta = {
        "title_ja": "",  # 日本語タイトルは Gemini に生成させる
        "original_title": entry.title,
        "url": url,
        "published": entry.pub_slash,
    }
    return PROMPT_TMPL.format(**meta)


# ========== Gemini 呼び出し ==========
def gemini_audio(mp3_bytes: bytes, prompt: str) -> str:
    """音声バイト列とプロンプトを渡し、Gemini Flash で要約を得る"""
    if len(mp3_bytes) > 20 * 1024 * 1024:  # 20MB 超は Files API
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
        return res.json()["candidates"][0]["content"]["parts"][0]["text"]

    raise RuntimeError("Gemini API failed after 5 retries")


# ========== YouTube 用 ==========
def yt_meta(url: str) -> dict:
    """yt-dlp -j --skip-download でメタデータ取得"""
    meta_json = subprocess.run(
        ["yt-dlp", "-j", "--skip-download", url],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(meta_json)


def yt_is_video(meta: dict) -> bool:
    """Shorts とライブ配信を除外"""
    dur, w, h = meta.get("duration", 0), meta.get("width", 0), meta.get("height", 0)
    shorts = dur <= 60 or (h and w and h > w)
    stream = (
        meta.get("is_live")
        or meta.get("was_live")
        or meta.get("live_status") in {"is_live", "was_live", "is_upcoming"}
    )
    return (not shorts) and (not stream)


# ========== Podcast 用 ==========
def is_podcast(entry) -> bool:
    """enclosure で audio/* を持つかどうか"""
    return any(
        l.get("rel") == "enclosure" or l.get("type", "").startswith("audio/")
        for l in entry.get("links", [])
    )


def fetch_enclosure(entry, dest: pathlib.Path):
    enc = next(l for l in entry.links if l.get("rel") == "enclosure")
    r = requests.get(enc.href, timeout=600)  # 大きいファイルもあるので長めタイムアウト
    r.raise_for_status()
    dest.write_bytes(r.content)


# ========== 処理ルーチン ==========
def process_youtube(entry):
    vid = entry.yt_videoid
    url = f"https://youtu.be/{vid}"
    if not yt_is_video(yt_meta(url)):
        print(f"   - SKIP non-video {vid}")
        return

    prompt = build_prompt(entry)

    with tempfile.TemporaryDirectory() as tmpdir:
        mp3_path = pathlib.Path(tmpdir) / f"{vid}.mp3"
        subprocess.run(
            ["yt-dlp", "-x", "--audio-format", "mp3", "-o", str(mp3_path), url],
            check=True,
        )
        md = gemini_audio(mp3_path.read_bytes(), prompt)

    author = sanitize_filename(getattr(entry, "author", "unknown"), 40)
    fname = f"{entry.pub_dash}_{sanitize_filename(entry.title)}_{author}.md"
    (OUT_YT / fname).write_text(md, encoding="utf-8")
    print(f" ✔ YT  {entry.title}")
    notify(f"YouTube: {entry.title}")


def process_podcast(entry):
    prompt = build_prompt(entry)

    print(f"[download] {entry.title}")

    with tempfile.TemporaryDirectory() as tmpdir:
        mp3_path = pathlib.Path(tmpdir) / "podcast.mp3"
        fetch_enclosure(entry, mp3_path)  # ← ここで進捗バーを表示
        md = gemini_audio(mp3_path.read_bytes(), prompt)

    author = sanitize_filename(
        getattr(entry, "author", getattr(entry, "itunes_author", "unknown")), 40
    )
    fname = f"{entry.pub_dash}_{sanitize_filename(entry.title)}_{author}.md"
    (OUT_POD / fname).write_text(md, encoding="utf-8")

    print(f" ✔ Pod  {entry.title}")  # YouTube と同じ形式
    notify(f"Podcast: {entry.title}")


# ========== クロール ==========
def crawl():
    since_ts = time.time() - WINDOW_HOURS * 3600
    feeds_raw = yaml.safe_load(pathlib.Path("feeds.yaml").read_text()) or []
    feeds = [f.strip() for f in feeds_raw if isinstance(f, str) and f.strip()]
    for feed_url in feeds:
        if not feed_url.strip():
            continue
        print(f"● {feed_url}")
        parsed = feedparser.parse(feed_url.strip())
        for e in parsed.entries:
            ts_tuple = getattr(e, "published_parsed", None) or getattr(
                e, "updated_parsed", None
            )
            if not ts_tuple or calendar.timegm(ts_tuple) < since_ts:
                continue

            pub_dt = datetime.fromtimestamp(calendar.timegm(ts_tuple), UTC)
            e.pub_dash = pub_dt.strftime("%Y-%m-%d")  # 例 2025-05-31 （ファイル名用）
            e.pub_slash = pub_dt.strftime("%Y/%m/%d")  # 例 2025/05/31 （YAML 用）

            if hasattr(e, "yt_videoid"):
                process_youtube(e)
            elif is_podcast(e):
                process_podcast(e)
            else:
                print(f"   - SKIP unknown type: {e.title}")

            time.sleep(3)  # API レート制限対策


if __name__ == "__main__":
    crawl()
