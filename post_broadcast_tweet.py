"""
毎週の放送後ツイート作成とPR動画探しを自動化するスクリプト（2026-10-18新設）。

【処理内容】
1. 配信URLの自動取得
   - ツイキャス: https://twitcasting.tv/tweettvjp/archive/ の録画一覧を新しい順に
     個別ページまで開き、視聴制限の無い最新録画を「一般」、メンバーシップ限定
     （個別ページに `tw-membership-gate`＝「メンバーシップ限定」ゲートがある）または
     合言葉付きの最新録画を「メンシプ」とする。一覧ページ自体には未ログイン時は
     限定バッジが出ない（実機確認済み）ため、一覧だけでは判定しない。
   - YouTube: 再生リストのytInitialData（lockupViewModel形式）を読み、先頭（新しい順）
     から「配信済み」と表示されている最初の動画を採用する（配信予定・配信中は除外）。
2. note記事と話題の抽出
   - WordPressに同期済みの最新note記事（noteカテゴリー、date降順）を使う。
     note.com側にそれより新しい記事があれば、未同期として警告する。
   - 本文の見出し（h2/h3/h4）のうち 🍀/🍁 で始まるものを話題として重複なく取り出す
     （目次のliにも同じ文言が重複して入っているため見出しのみを使う）。
     「※話題の概要※」のような固定の案内見出しは除外する。
3. PR動画のサジェスト
   - C:\\Users\\junji\\Downloads 直下の動画のうち、ファイル名が「PR」で始まる、
     または英字に挟まれていない「PR」を含むものを候補にする（ランダムなID名
     例: EyaqgV8ahyPRRTIi.mp4 を誤って拾わないため）。
   - 形態素解析ライブラリは使わず、漢字・カタカナ・英数字の連続部分から作った
     2文字の並び（バイグラム）の共通数でスコアを付ける。「解説」「動画」等、
     どのPR動画名にも付く語は除外する。
4. ツイート文を画面に表示し、tweet_drafts/放送後ツイート_YYYYMMDD.txt にも保存する。
   【仕様】投稿先のXは認証済みプレミアムアカウント（長文投稿可）のため、280文字制限・
   文字数警告・話題の切り詰めは一切行わず、抽出した全話題（🍀/🍁の行）を常にフルで出力する。

実行方法:
    python post_broadcast_tweet.py
    （または「放送後ツイート作成.bat」をダブルクリック）
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import requests
from bs4 import BeautifulSoup

import auto_sync_blogs as core

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "tweet_drafts"

TWITCASTING_USER = "tweettvjp"
TWITCASTING_ARCHIVE_URL = f"https://twitcasting.tv/{TWITCASTING_USER}/archive/"
TWITCASTING_MAX_MOVIES_TO_CHECK = 10
YOUTUBE_PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLAkyzn8bbDsf8WrApMMzogAIatViVn-Gt"
PR_VIDEO_DIR = Path(r"C:\Users\junji\Downloads")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv"}
TOPIC_MARKS = ("🍀", "🍁")
# 話題ではない固定の案内見出し（この文字列を含む見出しは除外）
TOPIC_EXCLUDE_PATTERNS = ("※話題の概要※",)
# PR動画名・話題に共通して頻出し、一致判定のノイズになる語
MATCH_STOPWORDS = ("動画解説", "音声解説", "総合", "解説", "動画", "音声", "ビデオ", "ショート", "ドン深闇")
MAX_SUGGESTIONS_PER_TOPIC = 3
MIN_MATCH_SCORE = 2
WEEKDAYS_JA = "月火水木金土日"

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ja"}


# ==================== 1. 配信URLの取得 ====================

def fetch_twitcasting_urls() -> tuple[dict | None, dict | None]:
    """(一般の最新録画, メンシプの最新録画) を {url, title, datetime} で返す。見つからなければNone。"""
    r = requests.get(TWITCASTING_ARCHIVE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    general = membership = None
    for a in soup.select("a.tw-movie-thumbnail2")[:TWITCASTING_MAX_MOVIES_TO_CHECK]:
        href = a.get("href") or ""
        if f"/{TWITCASTING_USER}/movie/" not in href:
            continue
        url = "https://twitcasting.tv" + href if href.startswith("/") else href
        title_el = a.select_one(".tw-movie-thumbnail2-title")
        time_el = a.select_one("time.tw-movie-thumbnail2-date")
        movie = {
            "url": url,
            "title": title_el.get_text(" ", strip=True) if title_el else "",
            "datetime": parse_twitcasting_datetime(time_el.get("datetime") if time_el else ""),
        }
        page = requests.get(url, headers=HEADERS, timeout=30).text
        is_limited = "tw-membership-gate" in page or "合言葉" in page
        if is_limited and membership is None:
            membership = movie
        elif not is_limited and general is None:
            general = movie
        if general and membership:
            break
    return general, membership


def parse_twitcasting_datetime(value: str) -> datetime | None:
    # 例: "Sat, 17 Oct 2026 21:44:29 +0900"
    try:
        return datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


def iter_lockups(obj):
    if isinstance(obj, dict):
        if "lockupViewModel" in obj:
            yield obj["lockupViewModel"]
            return
        for v in obj.values():
            yield from iter_lockups(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_lockups(v)


def collect_texts(obj, acc: list[str]) -> list[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("content", "text") and isinstance(v, str):
                acc.append(v)
            else:
                collect_texts(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_texts(v, acc)
    return acc


def fetch_latest_youtube_video() -> dict | None:
    r = requests.get(YOUTUBE_PLAYLIST_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", r.text, re.S)
    if not m:
        return None
    for lockup in iter_lockups(json.loads(m.group(1))):
        md = (lockup.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        meta_texts = collect_texts(md.get("metadata") or {}, [])
        if not any("配信済み" in t for t in meta_texts):
            continue  # 配信予定・配信中・通常アップロード動画は対象外
        return {
            "url": f"https://www.youtube.com/watch?v={lockup['contentId']}",
            "title": (md.get("title") or {}).get("content", ""),
            "when": next((t for t in meta_texts if "配信済み" in t), ""),
        }
    return None


# ==================== 2. note記事と話題の抽出 ====================

def fetch_latest_synced_note(wp: core.WP) -> dict | None:
    r = wp._request(
        "GET", f"{wp.site_url}/wp-json/wp/v2/posts",
        params={"categories": core.NOTE_CATEGORY_ID, "per_page": 1, "orderby": "date", "order": "desc",
                "context": "edit", "_fields": "id,slug,date,link,content,meta"},
    )
    posts = r.json()
    if not posts:
        return None
    p = posts[0]
    return {
        "key": p["slug"],
        # note記事のスラッグ＝note.comの記事キー（"n"始まり）。CLAUDE.md 5項のスラッグ規則
        "url": f"https://note.com/{core.NOTE_USERNAME}/n/{p['slug']}",
        "date": p["date"],
        "title": (p.get("meta") or {}).get("note_full_title") or "",
        "content": p["content"]["raw"],
    }


def extract_topics(content_html: str) -> list[str]:
    soup = BeautifulSoup(content_html, "html.parser")
    topics: list[str] = []
    for el in soup.find_all(["h2", "h3", "h4"]):
        text = el.get_text(" ", strip=True)
        if not text.startswith(TOPIC_MARKS):
            continue
        if any(p in text for p in TOPIC_EXCLUDE_PATTERNS):
            continue
        if text not in topics:
            topics.append(text)
    return topics


# ==================== 3. PR動画のサジェスト ====================

PR_NAME_PATTERN = re.compile(r"^PR|(?<![A-Za-z])PR(?![A-Za-z])")
CONTENT_RUN_PATTERN = re.compile(r"[一-龯々〆ヵヶ]+|[ァ-ヴー]+|[A-Za-z0-9]+")


def list_pr_videos() -> list[Path]:
    if not PR_VIDEO_DIR.exists():
        return []
    videos = [
        p for p in PR_VIDEO_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS and PR_NAME_PATTERN.search(p.stem)
    ]
    # 同点の候補では新しいファイルを優先して見せるため、更新日時の新しい順に並べておく
    return sorted(videos, key=lambda p: p.stat().st_mtime, reverse=True)


def keyword_bigrams(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"^PR", "", text)
    for w in MATCH_STOPWORDS:
        text = text.replace(w, " ")
    grams: set[str] = set()
    for run in CONTENT_RUN_PATTERN.findall(text):
        run = run.lower()
        if len(run) == 1:
            continue
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    return grams


def suggest_pr_videos(topic: str, videos: list[Path]) -> list[tuple[Path, int]]:
    topic_grams = keyword_bigrams(topic)
    scored = []
    for v in videos:
        score = len(topic_grams & keyword_bigrams(v.stem))
        if score >= MIN_MATCH_SCORE:
            scored.append((v, score))
    scored.sort(key=lambda x: -x[1])  # 安定ソートのため同点は新しいファイル順のまま
    return scored[:MAX_SUGGESTIONS_PER_TOPIC]


# ==================== 4. 出力 ====================

def format_date_ja(dt: datetime) -> str:
    return f"{dt.month}/{dt.day}（{WEEKDAYS_JA[dt.weekday()]}）"


def main() -> None:
    warnings: list[str] = []

    print("ツイキャスの録画一覧を確認しています...")
    try:
        tc_general, tc_member = fetch_twitcasting_urls()
    except requests.RequestException as e:
        tc_general = tc_member = None
        warnings.append(f"ツイキャスの録画一覧を取得できませんでした: {e}")
    if tc_general is None:
        warnings.append("ツイキャス一般配信の録画が見つかりませんでした")
    if tc_member is None:
        warnings.append("ツイキャス・メンシプ配信の録画が見つかりませんでした")

    print("YouTubeの再生リストを確認しています...")
    try:
        yt = fetch_latest_youtube_video()
    except requests.RequestException as e:
        yt = None
        warnings.append(f"YouTubeの再生リストを取得できませんでした: {e}")
    if yt is None:
        warnings.append("YouTubeの配信済み動画が見つかりませんでした")

    print("WordPressに同期済みの最新note記事を確認しています...")
    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    note = fetch_latest_synced_note(wp)
    topics: list[str] = []
    if note is None:
        warnings.append("WordPressに同期済みのnote記事が見つかりませんでした")
    else:
        topics = extract_topics(note["content"])
        if not topics:
            warnings.append("note記事から🍀/🍁で始まる話題の見出しが見つかりませんでした")
        try:
            latest_on_note = core.fetch_note_article_list()[0]
            if latest_on_note["key"] != note["key"]:
                warnings.append(
                    f"note.comにWordPress未同期の新しい記事があります（{latest_on_note['title'][:40]}…）。"
                    "週次実行.bat か 個別変更リスト.bat で同期してから再実行してください"
                )
        except (requests.RequestException, IndexError, KeyError) as e:
            warnings.append(f"note.com側の最新記事を確認できませんでした: {e}")

    # 放送日はツイキャス一般配信の日付を正とし、取れなければnote記事の公開日・今日の順に使う
    if tc_general and tc_general["datetime"]:
        broadcast_date = tc_general["datetime"]
    elif note:
        broadcast_date = datetime.fromisoformat(note["date"])
    else:
        broadcast_date = datetime.now()

    missing = "（取得できませんでした：手動で記入してください）"
    tweet_lines = [
        format_date_ja(broadcast_date),
        "🍀ツイキャス一般配信‼",
        tc_general["url"] if tc_general else missing,
        "🍀YouTube 忍者トゥルーサー",
        yt["url"] if yt else missing,
        "🍁ツイキャス・メンシプ配信‼",
        tc_member["url"] if tc_member else missing,
        "順ちゃんワールド・レポートnote版",
        note["url"] if note else missing,
        *topics,
    ]

    videos = list_pr_videos()
    suggest_lines = []
    for topic in topics:
        cands = suggest_pr_videos(topic, videos)
        if cands:
            names = " / ".join(f"{v.name}（一致度{s}）" for v, s in cands)
            suggest_lines.append(f"・{topic} -> 候補: {names}")
        else:
            suggest_lines.append(f"・{topic} -> 候補: なし（手動で確認してください）")

    output = "\n".join(tweet_lines) + "\n\n---\n【PR動画のサジェスト候補】\n"
    output += "\n".join(suggest_lines) if suggest_lines else "（話題が無いためサジェストできません）"
    output += f"\n\n（{PR_VIDEO_DIR} 内のPR動画 {len(videos)}件から検索）\n"
    if warnings:
        output += "\n【注意】\n" + "\n".join(f"・{w}" for w in warnings) + "\n"

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"放送後ツイート_{broadcast_date:%Y%m%d}.txt"
    out_path.write_text(output, encoding="utf-8")

    print("\n" + "=" * 60)
    print(output)
    print("=" * 60)
    print(f"保存しました: {out_path}")
    if warnings:
        sys.exit(1)


if __name__ == "__main__":
    main()
