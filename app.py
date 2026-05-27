import streamlit as st
import sqlite3
import re
import json
import html as html_lib
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
from collections import Counter
import hmac

DB_PATH = "notes.db"

NOTE_URL_RE = re.compile(r'https?://note\.com/[^/"\s]+/n/[a-zA-Z0-9]+')
NOTE_KEY_RE = re.compile(r'^[a-zA-Z0-9]{8,}$')

# note 記事オブジェクトだけが持つフィールド（ユーザー/タグオブジェクトとの区別用）
_NOTE_FIELDS = frozenset({
    "price", "publishAt", "publishedAt", "likeCount", "commentCount",
    "noteType", "canRead", "isLiked", "eyecatchUrl",
})


# ─────────────────────────────────────────────────────────────────────────────
# データクラス
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NoteItem:
    title: str
    date_str: str = ""
    price_str: str = ""


@dataclass
class ExtractionReport:
    li_blocks: int = 0   # Strategy E: <li> ブロック解析
    nextdata: int = 0    # Strategy A: __NEXT_DATA__
    regex: int = 0       # Strategy C: 正規表現
    bs4: int = 0         # Strategy D: BeautifulSoup <a> タグ
    page_owner: str = ""
    logs: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# DB 操作
# ─────────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT UNIQUE NOT NULL,
            title         TEXT NOT NULL,
            created_at_str TEXT DEFAULT '',
            price         TEXT DEFAULT '',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON notes(title)")
    # 既存 DB へのマイグレーション（カラムがなければ追加）
    for ddl in [
        "ALTER TABLE notes ADD COLUMN created_at_str TEXT DEFAULT ''",
        "ALTER TABLE notes ADD COLUMN price TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass  # すでに存在
    conn.commit()
    conn.close()


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/").split("#")[0].split("?")[0]


def upsert_note(url: str, item: NoteItem) -> str:
    url = normalize_url(url)
    title = item.title.strip()
    if not title:
        return "skipped"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT title, created_at_str, price FROM notes WHERE url = ?", (url,)
    )
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO notes (url, title, created_at_str, price) VALUES (?, ?, ?, ?)",
            (url, title, item.date_str, item.price_str),
        )
        result = "added"
    elif (row[0], row[1] or "", row[2] or "") != (title, item.date_str, item.price_str):
        cur.execute(
            """UPDATE notes
               SET title=?, created_at_str=?, price=?, updated_at=CURRENT_TIMESTAMP
               WHERE url=?""",
            (title, item.date_str, item.price_str, url),
        )
        result = "updated"
    else:
        result = "skipped"
    conn.commit()
    conn.close()
    return result


def delete_note(url: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM notes WHERE url = ?", (normalize_url(url),))
    conn.commit()
    conn.close()


def delete_all_notes():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM notes")
    conn.commit()
    conn.close()


def search_notes(query: str, sort_asc: bool = False) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title, created_at_str, price FROM notes WHERE title LIKE ?",
        (f"%{query}%",),
    )
    rows = cur.fetchall()
    conn.close()
    rows.sort(
        key=lambda r: _parse_date_for_sort(r[2] or ""),
        reverse=not sort_asc,   # True = 新しい順（降順）
    )
    return rows


def get_all_notes(limit: int = 500, sort_asc: bool = False) -> list[tuple[str, str, str, str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT url, title, created_at_str, price FROM notes LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    rows.sort(
        key=lambda r: _parse_date_for_sort(r[2] or ""),
        reverse=not sort_asc,
    )
    return rows


def count_notes() -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM notes")
    return cur.fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# ユーティリティ
# ─────────────────────────────────────────────────────────────────────────────

def format_note_date(raw: str) -> str:
    """ISO 8601 → '2025年12月20日 13:00' 形式"""
    if not raw:
        return ""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})', raw)
    if m:
        return (
            f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
            f" {m.group(4)}:{m.group(5)}"
        )
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', raw)
    if m:
        return f"{m.group(1)}年{int(m.group(2))}月{int(m.group(3))}日"
    return raw


def format_note_price(price) -> str:
    """int / None → '無料' / '¥100' 形式"""
    if price is None:
        return ""
    try:
        p = int(price)
        return "無料" if p == 0 else f"¥{p:,}"
    except (ValueError, TypeError):
        return str(price)


def clean_title(text: str) -> str:
    if not text:
        return ""
    if "\\u" in text or "\\n" in text or '\\"' in text:
        try:
            text = json.loads(f'"{text}"')
        except (json.JSONDecodeError, ValueError):
            text = re.sub(
                r'\\u([0-9a-fA-F]{4})',
                lambda m: chr(int(m.group(1), 16)),
                text,
            )
    text = html_lib.unescape(text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'[\r\n\t]+', ' ', text)
    return re.sub(r' {2,}', ' ', text).strip()


def _parse_date_for_sort(date_str: str) -> str:
    """
    '2026年5月26日 23:59' → '2026-05-26 23:59' に変換してソートキーとして返す。
    日付なし・不正な場合は '0000-00-00 00:00'（末尾に並ぶ）。
    """
    if not date_str:
        return "0000-00-00 00:00"
    m = re.match(r'(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{2}:\d{2}))?', date_str)
    if m:
        return (
            f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
            f" {m.group(4) or '00:00'}"
        )
    return "0000-00-00 00:00"


def _highlight(text: str, query: str) -> str:
    """
    text 内の query キーワードを <mark> タグで囲む（大文字小文字無視）。
    XSS を防ぐため、テキストと query の両方を HTML エスケープしてから処理する。
    """
    if not query or not text:
        return html_lib.escape(text)
    escaped_text  = html_lib.escape(text)
    escaped_query = re.escape(html_lib.escape(query))
    return re.sub(
        f"({escaped_query})",
        r"<mark>\1</mark>",
        escaped_text,
        flags=re.IGNORECASE,
    )


def _clean_aria_label(aria: str) -> str:
    """
    aria-label からタイトル以降のステータス・日付を除去してフルタイトルを取り出す。
    例: "タイトル 公開中 2026年5月26日 23:59" → "タイトル"
         "タイトル 下書き"                     → "タイトル"
    """
    # ステータスワード＋後続する日付文字列をまとめて除去
    cleaned = re.sub(
        r'\s+(公開中|下書き|予約中|限定公開|有料限定|メンバーシップ限定)'
        r'(?:\s+\d{4}年\d{1,2}月\d{1,2}日.*)?$',
        '',
        aria,
        flags=re.DOTALL,
    )
    # 上記で消えなかった末尾の日付パターンを除去
    cleaned = re.sub(r'\s+\d{4}年\d{1,2}月\d{1,2}日[\d\s:]*$', '', cleaned)
    return cleaned.strip()


def _make_note_item(obj: dict, title: str) -> NoteItem:
    """JSON オブジェクトから NoteItem を生成"""
    raw_date = (
        obj.get("publishAt") or obj.get("publishedAt")
        or obj.get("createdAt") or obj.get("created_at") or ""
    )
    return NoteItem(
        title=title,
        date_str=format_note_date(str(raw_date)) if raw_date else "",
        price_str=format_note_price(obj.get("price")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 抽出エンジン
# ─────────────────────────────────────────────────────────────────────────────

def extract_from_html(raw_html: str) -> tuple[list[tuple[str, NoteItem]], ExtractionReport]:
    """
    note.com の HTML ソースから (url, NoteItem) ペアを全件抽出する。

    Strategy E: <li> ブロック解析（クリエイターダッシュボード専用・最優先）
    Strategy A: __NEXT_DATA__ JSON パース（Next.js 公開ページ本命）
    Strategy B: <script type="application/json"> タグ群
    Strategy C: 正規表現ベタ掘り
    Strategy D: BeautifulSoup <a> タグフォールバック
    """
    results: dict[str, NoteItem] = {}
    report = ExtractionReport()

    # ── Strategy E を最初に実行（最も精度が高い専用ロジック）────────────────
    report.li_blocks = _strategy_li_blocks(raw_html, results, report)

    # ── 以下は補完フォールバック ─────────────────────────────────────────────
    before = len(results)
    _strategy_nextdata(raw_html, results, report)
    # nextdata はフォールバック分のみカウント（E との重複は results dict が防ぐ）
    report.nextdata = len(results) - before

    _strategy_script_json(raw_html, results, report)

    before = len(results)
    _strategy_regex(raw_html, results, report)
    report.regex = len(results) - before

    before = len(results)
    _strategy_bs4_links(raw_html, results, report)
    report.bs4 = len(results) - before

    return list(results.items()), report


# ── urlname ヘルパー ──────────────────────────────────────────────────────────

def _collect_urlnames(obj, bucket: list, depth: int = 0):
    if depth > 30:
        return
    if isinstance(obj, dict):
        v = obj.get("urlname")
        if isinstance(v, str) and v:
            bucket.append(v)
        for child in obj.values():
            _collect_urlnames(child, bucket, depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_urlnames(item, bucket, depth + 1)


def _dominant_urlname(data) -> str:
    """JSON 全体で最多出現の urlname を返す（ページオーナー推定）"""
    bucket: list[str] = []
    _collect_urlnames(data, bucket)
    return Counter(bucket).most_common(1)[0][0] if bucket else ""


def _is_note_object(obj: dict) -> bool:
    """note 記事に固有のフィールドを持つか確認（ユーザー/タグ等の誤検知抑制）"""
    return bool(_NOTE_FIELDS & obj.keys())


# ── Strategy A ───────────────────────────────────────────────────────────────

def _strategy_nextdata(raw_html: str, results: dict, report: ExtractionReport):
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        raw_html,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        report.logs.append("Strategy A: __NEXT_DATA__ が見つかりませんでした")
        return
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        report.logs.append(f"Strategy A: JSON パース失敗 — {e}")
        return

    page_owner = _dominant_urlname(data)
    report.page_owner = page_owner
    report.logs.append(f"Strategy A: ページオーナー推定 → '{page_owner}'")

    before = len(results)
    _walk_json(data, results, depth=0, ctx_urlname=page_owner, page_owner=page_owner)
    report.nextdata = len(results) - before
    report.logs.append(f"Strategy A (__NEXT_DATA__): {report.nextdata} 件抽出")


# ── Strategy B ───────────────────────────────────────────────────────────────

def _strategy_script_json(raw_html: str, results: dict, report: ExtractionReport):
    soup = BeautifulSoup(raw_html, "html.parser")
    count = 0
    for script in soup.find_all("script", type="application/json"):
        text = script.get_text(strip=True)
        if not text or "note.com" not in text:
            continue
        try:
            data = json.loads(text)
            owner = _dominant_urlname(data) or report.page_owner
            before = len(results)
            _walk_json(data, results, depth=0, ctx_urlname=owner, page_owner=owner)
            count += len(results) - before
        except (json.JSONDecodeError, ValueError):
            pass
    if count:
        report.logs.append(f"Strategy B (script[type=json]): {count} 件追加")


# ── Strategy C ───────────────────────────────────────────────────────────────

def _strategy_regex(raw_html: str, results: dict, report: ExtractionReport):
    username_candidates = re.findall(r'"urlname"\s*:\s*"([^"]{1,50})"', raw_html)
    page_owner = Counter(username_candidates).most_common(1)[0][0] if username_candidates else ""
    if not report.page_owner:
        report.page_owner = page_owner

    # ─ C-1: "key" + "name" ペア（近傍チャンク内に記事固有フィールドが必要）─
    for m in re.finditer(r'"key"\s*:\s*"([a-zA-Z0-9]{8,})"', raw_html):
        key = m.group(1)
        start = max(0, m.start() - 200)
        end = min(len(raw_html), m.end() + 2000)
        chunk = raw_html[start:end]

        # note 固有フィールドがないチャンクは誤検知の可能性が高いのでスキップ
        if not re.search(
            r'"(publishAt|publishedAt|likeCount|price|noteType|canRead|eyecatchUrl)"',
            chunk,
        ):
            continue

        nm = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.){5,})"', chunk)
        if not nm:
            continue
        title = clean_title(nm.group(1))
        if not title or len(title) < 5:
            continue

        un_m = re.search(r'"urlname"\s*:\s*"([^"]{1,50})"', chunk)
        uname = un_m.group(1) if un_m else page_owner
        # 別ユーザーの推薦記事はスキップ
        if page_owner and uname and uname != page_owner:
            continue
        if not uname:
            continue

        date_m = re.search(
            r'"(?:publishAt|publishedAt|createdAt)"\s*:\s*"([^"]+)"', chunk
        )
        price_m = re.search(r'"price"\s*:\s*(\d+)', chunk)

        url = normalize_url(f"https://note.com/{uname}/n/{key}")
        if url not in results:
            results[url] = NoteItem(
                title=title,
                date_str=format_note_date(date_m.group(1)) if date_m else "",
                price_str=format_note_price(int(price_m.group(1))) if price_m else "",
            )

    # ─ C-2: URL が直接テキストに出現する場合 ─
    for url_match in NOTE_URL_RE.finditer(raw_html):
        url = normalize_url(url_match.group(0))
        if url in results:
            continue
        url_user = re.search(r'note\.com/([^/]+)/n/', url)
        if url_user and page_owner and url_user.group(1) != page_owner:
            continue
        after = raw_html[url_match.end(): url_match.end() + 500]
        nm = re.search(r'"name"\s*:\s*"((?:[^"\\]|\\.){5,})"', after)
        if not nm:
            continue
        title = clean_title(nm.group(1))
        date_m = re.search(r'"(?:publishAt|publishedAt)"\s*:\s*"([^"]+)"', after)
        price_m = re.search(r'"price"\s*:\s*(\d+)', after)
        results[url] = NoteItem(
            title=title,
            date_str=format_note_date(date_m.group(1)) if date_m else "",
            price_str=format_note_price(int(price_m.group(1))) if price_m else "",
        )

    # ─ C-3: href="..." > タイトル </a> パターン ─
    for m in re.finditer(
        r'href=["\'](' + NOTE_URL_RE.pattern + r')["\'][^>]*>([^<]{10,})</a>',
        raw_html,
        re.IGNORECASE,
    ):
        url = normalize_url(m.group(1))
        url_user = re.search(r'note\.com/([^/]+)/n/', url)
        if url_user and page_owner and url_user.group(1) != page_owner:
            continue
        title = clean_title(m.group(2))
        if url not in results and title:
            results[url] = NoteItem(title=title)


# ── Strategy D ───────────────────────────────────────────────────────────────

def _strategy_bs4_links(raw_html: str, results: dict, report: ExtractionReport):
    page_owner = report.page_owner
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup.find_all("a", href=True):
        href: str = tag["href"]
        if not NOTE_URL_RE.search(href):
            continue
        url = normalize_url(href)
        if url in results:
            continue
        url_user = re.search(r'note\.com/([^/]+)/n/', url)
        if url_user and page_owner and url_user.group(1) != page_owner:
            continue

        title = tag.get_text(" ", strip=True)
        if len(title) < 10:
            title = tag.get("aria-label", "") or tag.get("title", "") or title
        if len(title) < 10 and tag.parent:
            title = tag.parent.get_text(" ", strip=True)
        title = clean_title(title)
        if title and len(title) >= 5:
            results[url] = NoteItem(title=title)


# ── Strategy E ───────────────────────────────────────────────────────────────

def _strategy_li_blocks(raw_html: str, results: dict, report: ExtractionReport) -> int:
    """
    note.com クリエイターダッシュボード固有の HTML を解析する。
    <li data-v-765c3831=""> または class="o-articleList__item" の各ブロックを
    1件ずつ切り出し、URL・フルタイトル・日付・価格を確実に抽出する。

    ● URL 取得の優先順位:
        1. <a class="o-articleList__link"> の href
        2. ブロック内任意の note.com 記事 href
        3. listCheckbox_{id} から記事ID を取得して URL を合成

    ● タイトル取得の優先順位:
        1. <a aria-label="..."> （フルタイトル+ステータス → クレンジングして取得）
        2. class="o-articleList__heading" のテキスト
        3. class="visually-hidden" の <span> テキスト
        4. <h1>〜<h4> / <p> の中で最も長いテキスト

    ● 日付: <time datetime="..."> 属性 → なければテキスト → format_note_date で整形
    ● 価格: 「¥」を含む <span> → なければ「無料」
    """
    soup = BeautifulSoup(raw_html, "html.parser")

    # ── <li> ブロックを探す ──────────────────────────────────────────────────
    li_items = soup.find_all("li", attrs={"data-v-765c3831": True})
    if not li_items:
        li_items = soup.find_all("li", class_="o-articleList__item")
    if not li_items:
        report.logs.append("Strategy E: <li> ブロックが見つかりませんでした")
        return 0

    # ── page_owner を <a href> から動的に取得 ───────────────────────────────
    page_owner = report.page_owner
    if not page_owner:
        for li in li_items:
            for a in li.find_all("a", href=True):
                href = a["href"]
                m = re.search(r'(?:note\.com)?/([a-zA-Z0-9_-]+)/n/n?[a-zA-Z0-9]', href)
                if m and m.group(1) not in ("", "n"):
                    page_owner = m.group(1)
                    report.page_owner = page_owner
                    break
            if page_owner:
                break
        # href から取れなかった場合は og:url / canonical から試みる
        if not page_owner:
            m = re.search(r'note\.com/([a-zA-Z0-9_-]+)/(?:all|n/)', raw_html)
            if m:
                page_owner = m.group(1)
                report.page_owner = page_owner
    report.logs.append(f"Strategy E: ページオーナー推定 → '{page_owner or '不明'}'")

    count = 0
    for li in li_items:

        # ── URL 取得 ────────────────────────────────────────────────────────
        url = ""

        # 優先①: class="o-articleList__link" の href
        a_link = li.find("a", class_="o-articleList__link")
        if not a_link:
            # class 名が複合でも部分一致で探す
            a_link = li.find(
                "a", class_=re.compile(r'o-articleList__link')
            )

        if a_link and a_link.get("href"):
            href = a_link["href"]
            if href.startswith("https://note.com"):
                url = normalize_url(href)
            elif href.startswith("/") and page_owner:
                url = normalize_url(f"https://note.com{href}")

        # 優先②: ブロック内の任意の note.com 記事 href
        if not url:
            for a in li.find_all("a", href=True):
                href = a["href"]
                if NOTE_URL_RE.search(href):
                    url = normalize_url(href)
                    a_link = a  # aria-label 取得のために保持
                    break

        # 優先③: listCheckbox_{id} から記事 ID を合成
        if not url:
            for tag in li.find_all(True):
                for attr_name in ("for", "id"):
                    val = tag.get(attr_name, "")
                    if val.startswith("listCheckbox_"):
                        article_id = val.replace("listCheckbox_", "")
                        if article_id.isdigit() and page_owner:
                            # note の記事キーは "n" + 数字
                            url = f"https://note.com/{page_owner}/n/n{article_id}"
                            break
                if url:
                    break

        if not url or not NOTE_URL_RE.search(url):
            continue

        # ── タイトル取得 ─────────────────────────────────────────────────────
        title = ""

        # 優先①: aria-label（クリエイターダッシュボードのフルタイトルはここに入る）
        if a_link:
            aria = a_link.get("aria-label", "")
            if aria:
                title = _clean_aria_label(aria)

        # a_link が取れなかった場合は aria-label を持つ任意の <a> を探す
        if not title:
            for a in li.find_all("a", attrs={"aria-label": True}):
                aria = a.get("aria-label", "")
                if aria:
                    title = _clean_aria_label(aria)
                    if title:
                        break

        # 優先②: class="o-articleList__heading"
        if not title:
            h = li.find(class_="o-articleList__heading")
            if h:
                title = h.get_text(" ", strip=True)

        # 優先③: class="visually-hidden" の span
        if not title:
            vh = li.find("span", class_="visually-hidden")
            if vh:
                title = vh.get_text(" ", strip=True)

        # 優先④: <h1>〜<h4> / <p> で最も長いテキスト
        if not title:
            for tag in li.find_all(["h1", "h2", "h3", "h4", "p"]):
                t = tag.get_text(" ", strip=True)
                if len(t) > len(title):
                    title = t

        title = clean_title(title)
        if not title:
            continue

        # ── 日付取得 ─────────────────────────────────────────────────────────
        date_str = ""
        time_tag = li.find("time")
        if time_tag:
            dt_attr = time_tag.get("datetime", "")
            if dt_attr:
                # datetime="2026-05-26T23:59:00+09:00" など ISO 形式
                date_str = format_note_date(dt_attr)
            if not date_str:
                # テキスト "2026年5月26日 23:59" をそのまま使う
                date_str = time_tag.get_text(strip=True)

        # ── 価格取得 ─────────────────────────────────────────────────────────
        price_str = ""
        for span in li.find_all("span"):
            t = span.get_text(strip=True)
            if "¥" in t:
                price_str = t
                break
        if not price_str:
            price_str = "無料"

        # ── 登録 ─────────────────────────────────────────────────────────────
        url = normalize_url(url)
        if url not in results:
            results[url] = NoteItem(
                title=title,
                date_str=date_str,
                price_str=price_str,
            )
            count += 1

    report.logs.append(f"Strategy E (<li>ブロック解析): {count} 件抽出")
    return count


# ── JSON ツリーウォーカー ────────────────────────────────────────────────────

def _walk_json(
    obj, results: dict, depth: int, ctx_urlname: str = "", page_owner: str = ""
):
    """
    JSON を再帰走査して note 記事を登録する。

    ctx_urlname: 親スコープから継承した urlname（先頭/featured 記事のフォールバック用）
    page_owner:  ページオーナーの urlname。他ユーザーの推薦記事を除外するために使用。
    """
    if depth > 30:
        return

    if isinstance(obj, dict):
        user = obj.get("user") or {}
        local_urlname = (
            (user.get("urlname", "") if isinstance(user, dict) else "")
            or obj.get("urlname", "")
        )
        effective_urlname = local_urlname or ctx_urlname

        key  = obj.get("key", "")
        name = obj.get("name", "")

        # 記事固有フィールドがないオブジェクトはスキップ（ユーザー/タグ等の誤検知抑制）
        if _is_note_object(obj):

            # パターン①: noteUrl フィールドが直接ある
            note_url_direct = obj.get("noteUrl", "") or obj.get("note_url", "")
            if note_url_direct and NOTE_URL_RE.search(note_url_direct) and name:
                url_u = re.search(r'note\.com/([^/]+)/n/', note_url_direct)
                if not (page_owner and url_u and url_u.group(1) != page_owner):
                    url = normalize_url(note_url_direct)
                    title = clean_title(name)
                    if title and url not in results:
                        results[url] = _make_note_item(obj, title)

            # パターン②: key + effective_urlname で URL を組み立て
            if key and NOTE_KEY_RE.match(str(key)) and name and effective_urlname:
                # local_urlname が明示的に別ユーザーを指している場合は除外
                if not (page_owner and local_urlname and local_urlname != page_owner):
                    url = normalize_url(
                        f"https://note.com/{effective_urlname}/n/{key}"
                    )
                    title = clean_title(name)
                    if title and url not in results:
                        results[url] = _make_note_item(obj, title)

        for v in obj.values():
            _walk_json(v, results, depth + 1, effective_urlname, page_owner)

    elif isinstance(obj, list):
        for item in obj:
            _walk_json(item, results, depth + 1, ctx_urlname, page_owner)


# ─────────────────────────────────────────────────────────────────────────────
# UI ヘルパー
# ─────────────────────────────────────────────────────────────────────────────

def _decode_uploaded_file(uploaded_file) -> str | None:
    """
    st.file_uploader の戻り値をテキストにデコードする。
    UTF-8(BOM付き含む) → Shift-JIS → EUC-JP → latin-1 の順で試みる。
    """
    raw_bytes = uploaded_file.read()
    for enc in ("utf-8-sig", "utf-8", "shift_jis", "euc-jp", "latin-1"):
        try:
            return raw_bytes.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    st.error(
        "ファイルのエンコードを判定できませんでした。"
        "UTF-8 形式で保存し直してから再試行してください。"
    )
    return None


def _run_extraction_ui(raw_html: str) -> None:
    """HTML テキストを解析・保存し、結果を Streamlit に表示する（共通処理）。"""
    with st.spinner("解析中..."):
        extracted, report = extract_from_html(raw_html)

    with st.expander("🔬 抽出エンジン 診断レポート"):
        st.write(f"- ページオーナー推定: **{report.page_owner or '不明'}**")
        st.write(f"- Strategy E (&lt;li&gt;ブロック解析): **{report.li_blocks}** 件　← ダッシュボードHTML専用")
        st.write(f"- Strategy A (__NEXT_DATA__):  **{report.nextdata}** 件（追加分）")
        st.write(f"- Strategy C (正規表現):        **{report.regex}** 件（追加分）")
        st.write(f"- Strategy D (BeautifulSoup):  **{report.bs4}** 件（追加分）")
        for log in report.logs:
            st.caption(log)

    if not extracted:
        st.warning(
            "note.com の記事が見つかりませんでした。\n\n"
            "- ブラウザで `https://note.com/{ユーザー名}/all` を開き、"
            "**Ctrl+U** でソースをコピーしていますか？\n"
            "- JavaScript 実行後の DOM ではなく、**生のHTMLソース**をコピーしていますか？\n"
            "- エキサイトブログ作成の HTML ファイルの場合、"
            "note.com へのリンクが `<a href=\"https://note.com/...\">`  形式で含まれていますか？"
        )
        return

    added = updated = skipped = 0
    for url, item in extracted:
        r = upsert_note(url, item)
        if r == "added":
            added += 1
        elif r == "updated":
            updated += 1
        else:
            skipped += 1

    st.success(
        f"✅ 処理完了 — 新規追加: **{added}** 件 ／ "
        f"更新: **{updated}** 件 ／ スキップ（重複）: **{skipped}** 件"
    )

    with st.expander(f"今回抽出した {len(extracted)} 件を確認"):
        for url, item in extracted:
            _render_note_row(
                url, item.title, item.date_str, item.price_str,
                show_delete=False,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 管理者認証ユーティリティ
# ─────────────────────────────────────────────────────────────────────────────

def _verify_password(password: str) -> bool:
    """st.secrets の ADMIN_PASSWORD と定数時間比較（タイミング攻撃対策）"""
    try:
        correct = st.secrets["ADMIN_PASSWORD"]
    except (KeyError, FileNotFoundError):
        return False
    return hmac.compare_digest(correct.encode("utf-8"), password.encode("utf-8"))


def _is_admin() -> bool:
    """現在のセッションが管理者としてログイン済みか返す"""
    return bool(st.session_state.get("is_admin", False))


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit UI
# ─────────────────────────────────────────────────────────────────────────────

init_db()

st.set_page_config(page_title="note インデックス検索", page_icon="📝", layout="wide")
st.title("📝 note インデックス検索システム")

st.markdown(
    """
    <style>
    .note-card {
        background: #f8f9fa;
        border-left: 4px solid #41b883;
        border-radius: 4px;
        padding: 10px 14px;
        margin-bottom: 2px;
        line-height: 1.7;
    }
    .note-card a {
        color: #1a1a1a;
        text-decoration: none;
        font-size: 0.95rem;
        word-break: break-all;
    }
    .note-card a:hover { color: #41b883; text-decoration: underline; }
    .note-meta { font-size: 0.78rem; color: #666; margin-top: 4px; }
    mark {
        background: linear-gradient(transparent 40%, #FFD700 40%);
        color: inherit;
        padding: 0 1px;
        border-radius: 2px;
        font-weight: bold;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── サイドバー：管理者ログイン ────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🔐 管理者ログイン")
    if st.session_state.get("is_admin"):
        st.success("✅ 管理者としてログイン中")
        if st.button("ログアウト", key="admin_logout"):
            st.session_state["is_admin"] = False
            st.rerun()
    else:
        _pw_input = st.text_input("パスワード", type="password", key="admin_pw_input")
        if st.button("ログイン", key="admin_login_btn"):
            if _verify_password(_pw_input):
                st.session_state["is_admin"] = True
                st.rerun()
            else:
                st.error("パスワードが違います")

# タブ表示：管理者のみ「データ追加」「管理」タブを表示
if _is_admin():
    tab_search, tab_add, tab_manage = st.tabs(["🔍 検索", "➕ データ追加", "⚙️ 管理"])
else:
    _tabs = st.tabs(["🔍 検索"])
    tab_search, tab_add, tab_manage = _tabs[0], None, None


def _render_note_row(
    url: str, title: str, date_str: str, price_str: str,
    show_delete: bool = True, highlight_query: str = "",
):
    """1件のノートカードを描画する（ハイライト・削除ボタン付き）"""
    meta_parts = []
    if date_str:
        meta_parts.append(f"📅 作成日: {date_str}")
    if price_str:
        meta_parts.append(f"💰 価格: {price_str}")
    meta_html = " &nbsp;|&nbsp; ".join(meta_parts)

    # タイトルにキーワードハイライトを適用（検索時のみ）
    display_title = _highlight(title, highlight_query)

    col_card, col_del = st.columns([11, 1])
    with col_card:
        st.markdown(
            f'<div class="note-card">'
            f'<a href="{url}" target="_blank" rel="noopener">'
            f'{display_title}</a>'
            + (f'<div class="note-meta">{meta_html}</div>' if meta_html else "")
            + "</div>",
            unsafe_allow_html=True,
        )
    with col_del:
        if show_delete:
            btn_key = f"del_{abs(hash(url)) % 10**12}"
            if st.button("🗑️", key=btn_key, help="この記事を削除"):
                delete_note(url)
                st.rerun()


# ── タブ①：検索 ──────────────────────────────────────────────────────────────
with tab_search:
    total = count_notes()
    st.caption(f"登録件数: {total:,} 件")

    # 検索入力 + 並び替えセレクタを横並びに配置
    col_q, col_sort = st.columns([4, 1])
    with col_q:
        query = st.text_input(
            "キーワードで検索（部分一致 / 200 文字以上のタイトルも対応）",
            placeholder="例：AI　生成　Python　など",
            key="search_query",
        )
    with col_sort:
        sort_label = st.radio(
            "並び替え",
            options=["📅 新しい順", "📅 古い順"],
            index=0,          # 初期値：新しい順
            horizontal=False,
            key="sort_order",
        )
    sort_asc = (sort_label == "📅 古い順")

    if query.strip():
        rows = search_notes(query.strip(), sort_asc=sort_asc)
        st.write(f"**{len(rows):,} 件**ヒット")
        if rows:
            for url, title, date_str, price_str in rows:
                _render_note_row(
                    url, title, date_str or "", price_str or "",
                    highlight_query=query.strip(),
                )
        else:
            st.info("一致する記事が見つかりませんでした。")
    else:
        if total == 0:
            st.info(
                "まだ記事が登録されていません。「データ追加」タブから"
                "HTMLを貼り付けて登録してください。"
            )
        else:
            with st.expander(f"全件表示（最新 500 件）", expanded=False):
                for url, title, date_str, price_str in get_all_notes(sort_asc=sort_asc):
                    _render_note_row(url, title, date_str or "", price_str or "")


# ── タブ②：データ追加 ─────────────────────────────────────────────────────────
if tab_add is not None:
    with tab_add:
        st.subheader("HTMLから記事を一括登録")

        # ════════════════════════════════════════════════════════════
        # 方法①: ファイルアップロード（10万文字以上の巨大ファイルに対応）
        # ════════════════════════════════════════════════════════════
        st.markdown("##### 📂 方法①: HTML / TXT ファイルをアップロード")
        st.caption(
            "エキサイトブログのHTML編集モードで作成した `.html` / `.txt` ファイルや、"
            "note.com のページソースをファイル保存したものをドラッグ＆ドロップしてください。"
            "10万文字以上の巨大ファイルにも対応しています。"
        )

        uploaded_file = st.file_uploader(
            "ファイルを選択またはドラッグ＆ドロップ",
            type=["html", "htm", "txt"],
            key="html_file_uploader",
            help="UTF-8 / Shift-JIS / EUC-JP 形式に対応",
        )

        if uploaded_file is not None:
            file_size_kb = len(uploaded_file.getvalue()) / 1024
            st.caption(
                f"📄 **{uploaded_file.name}** "
                f"（{file_size_kb:,.1f} KB / "
                f"{len(uploaded_file.getvalue()):,} bytes）を読み込み済み"
            )
            if st.button(
                "ファイルから抽出して登録する",
                type="primary",
                key="btn_process_file",
            ):
                raw_html = _decode_uploaded_file(uploaded_file)
                if raw_html:
                    _run_extraction_ui(raw_html)

        st.divider()

        # ════════════════════════════════════════════════════════════
        # 方法②: テキスト貼り付け（ブラウザの Ctrl+U ソース等）
        # ════════════════════════════════════════════════════════════
        st.markdown("##### 📋 方法②: HTMLソースを直接貼り付け")
        st.caption(
            "`https://note.com/{ユーザー名}/all` を **Ctrl+U** で開いた生のHTMLソース、"
            "またはエキサイトブログ記事本文のHTMLをそのまま貼り付けてください。"
        )

        html_input = st.text_area(
            "HTMLソースをここに丸ごと貼り付け",
            height=260,
            placeholder=(
                '<script id="__NEXT_DATA__" ...> を含む note.com の生 HTML\n'
                "または エキサイトブログの記事本文 HTML をそのまま貼り付けてください"
            ),
            key="html_paste_input",
        )

        if st.button("貼り付けから抽出して登録する", type="primary", key="btn_process_paste"):
            if not html_input.strip():
                st.error("HTMLを入力してください。")
            else:
                _run_extraction_ui(html_input)


# ── タブ③：管理 ───────────────────────────────────────────────────────────────
if tab_manage is not None:
    with tab_manage:
        st.subheader("データベース管理")

        # 削除後の成功メッセージ（session_state 経由で rerun をまたいで表示）
        if st.session_state.get("manage_msg"):
            st.success(st.session_state.pop("manage_msg"))

        st.metric("現在の登録件数", f"{count_notes():,} 件")

        st.divider()
        st.markdown("#### ⚠️ 全データ削除（初期化）")
        st.warning(
            "この操作は取り消せません。データベース内の全記事が完全に削除されます。"
        )

        confirm_delete = st.checkbox("本当にすべてのデータを削除することを確認しました")

        if st.button(
            "🗑️ 全データを削除する",
            type="primary",
            disabled=not confirm_delete,
        ):
            delete_all_notes()
            st.session_state["manage_msg"] = "✅ 全データを削除しました。"
            st.rerun()
