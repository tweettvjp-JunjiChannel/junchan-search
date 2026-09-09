"""
note / エキサイトブログ（rakusen.exblog.jp）とWordPress（junchan-world.com）を
毎日自動で同期させるための統合スクリプト。

【このスクリプトが行う4つの処理】（タスクスケジューラ等で1日1回 --execute 付きで実行する想定）

  1. note 新着記事の自動投稿
     note.com公開API（ログイン不要）で記事一覧を取得し、WordPress側に
     まだ存在しない記事（スラッグ "n{key}"）だけをPlaywright（保存済みログイン
     セッション）で本文取得し、Codocの有料壁ブロックを挿入した上でWordPressへ
     新規投稿する。既に存在するスラッグはスキップする（重複投稿防止）。

  2. エキサイトブログ新着記事の自動投稿
     rakusen.exblog.jp は完全公開ブログのためログイン不要。月別アーカイブから
     記事URLを収集し、WordPress側に存在しない記事（スラッグ "exblog-{id}"）を
     新規投稿する（カテゴリー「エキサイトブログ」+ exblog側のカテゴリー）。

  3. note記事の更新追従（公開から30日以内のみ）
     note.com公開APIの記事一覧を再取得し、「公開から30日以内」の記事について
     本文HTMLを取得し直す。前回取得時の本文ハッシュ（state.jsonに保存）と比較し、
     差分があればWordPress側の該当記事のcontentを上書き更新する（価格・アイキャッチ
     ・投稿日時は変更しない。Codoc価格ブロックは既存の値を維持したまま本文だけ差し替える）。

  4. 過去記事のCodoc自動値下げ（投稿から90日以上経過）
     WordPress記事のcontent内に埋め込まれたCodocブロック
     （<!-- wp:codoc/codoc-block {"price":N,...} -->）を直接読み書きする。
     投稿日から90日以上経過し、price > 100 の記事は price を100に書き換えて
     本文を再保存する（＝WordPress側の販売価格のみを値下げする）。
     note本家（note.com）側の価格には一切触れない。
     ※ Codocの管理画面（codoc.jp）を直接操作するのではなく、WordPress記事の
       content内のCodocブロック属性を書き換えて保存することで、codocプラグインの
       save_postフック経由でCodoc側にも自動反映される仕組みを利用している
       （wp_bulk_resave.py で確認済みの「無変更のまま再保存するとcodocと同期される」
       挙動と同じ経路）。この方式はブラウザ自動化（Playwright手動ログイン）が不要で、
       REST APIのみで完結するためタスクスケジューラでの完全無人実行に向いている。

【安全設計】
    - デフォルトはドライラン（何も変更しない。検出結果の一覧表示のみ）。
      実際に変更するには --execute を指定する。
    - --only new-note / new-exblog / update-note / discount-codoc で
      処理を個別に実行できる（省略時は全て実行）。
    - 各処理の結果は auto_sync_blogs_log.csv に追記される。
    - state.json（既定: auto_sync_state.json）に、note記事の本文ハッシュと
      価格を記録し、2回目以降の実行で「前回との差分」を判定する。

【前提条件】
    - wp_credentials.json が設定済みであること（WordPress REST API用）。
    - note.comへのログイン済みセッションが chrome_user_data/ に保存済みであること
      （note_downloader.py 等で一度手動ログインしておく。本文取得にのみ必要）。
    - Codoc価格ブロックの仕様は generate_perfect_wp_xml.py の
      CODOC_BLOCK_ATTRS_TEMPLATE / make_codoc_block() と共通。

実行方法:
    python auto_sync_blogs.py                                   # ドライラン（全処理、変更なし）
    python auto_sync_blogs.py --execute                         # 本番実行（全処理）
    python auto_sync_blogs.py --execute --only discount-codoc    # 値下げ処理のみ本番実行
    python auto_sync_blogs.py --execute --limit 5                # 各処理を最大5件までに制限（試験実行用）
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Windows既定のコンソールエンコーディング（cp932）では記事タイトル中の絵文字が
# 表示できずクラッシュする（実機で確認済み）。タスクスケジューラでの無人実行が
# 途中で落ちないよう、標準出力をUTF-8に固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from link_converter import rebuild_toc
from internal_article_links import (
    NOTE_CATEGORY_ID,
    convert_internal_links,
    fetch_link_maps,
    find_error_text_markers,
)

# ==================== 設定 ====================
SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPT_DIR / "wp_credentials.json"
STATE_PATH = SCRIPT_DIR / "auto_sync_state.json"
LOG_PATH = SCRIPT_DIR / "auto_sync_blogs_log.csv"
CHROME_USER_DATA_DIR = SCRIPT_DIR / "chrome_user_data"

NOTE_USERNAME = "tweettv"
NOTE_CONTENTS_API_URL = f"https://note.com/api/v2/creators/{NOTE_USERNAME}/contents"
NOTE_PROFILE_URL = f"https://note.com/{NOTE_USERNAME}"

EXBLOG_BASE_URL = "https://rakusen.exblog.jp"
EXBLOG_CATEGORY_ID = 2445  # WordPress側の「エキサイトブログ」カテゴリー
EXBLOG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

NOTE_UPDATE_WINDOW_DAYS = 30      # note側の更新追従の対象期間
CODOC_DISCOUNT_AFTER_DAYS = 90    # Codoc自動値下げの対象年数
CODOC_DISCOUNT_PRICE = 100        # 値下げ後の価格

# Codocブロック（generate_perfect_wp_xml.py の CODOC_BLOCK_ATTRS_TEMPLATE と共通仕様）
CODOC_BLOCK_ATTRS_TEMPLATE = {
    "showPrice": True,
    "price": 0,
    "limited": False,
    "limitedCount": 10,
    "affiliateMode": False,
    "affiliateRate": "0.0500",
    "showSupport": False,
    "showPaywalledSupport": False,
    "statusLimited": False,
    "subscriptions": {"4133": True},
    "version": "0.9.60",
}
CODOC_BLOCK_PATTERN = re.compile(
    r'<!--\s*wp:codoc/codoc-block\s*(\{.*?\})\s*-->.*?<!--\s*/wp:codoc/codoc-block\s*-->',
    re.DOTALL,
)
CODOC_TITLE_MAX_LENGTH = 95

REQUEST_DELAY_SECONDS = 1.0


# ==================== 共通ユーティリティ ====================

def load_credentials() -> dict:
    if not CREDENTIALS_PATH.exists():
        raise SystemExit(f"[エラー] {CREDENTIALS_PATH} が見つかりません。")
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    for key in ("site_url", "username", "application_password"):
        if not data.get(key) or "ここに" in data[key]:
            raise SystemExit(f"[エラー] wp_credentials.json の '{key}' が未設定です。")
    return data


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"note": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


VOLATILE_CONTENT_PATTERNS = [
    # note.com本文中のTwitter/X埋め込みウィジェット（iframe src）には、ページを
    # 読み込むたびに変わるセッションIDが含まれる。これを正規化しないと、記事の
    # 実際の編集が無くても再取得のたびに別ハッシュになり「更新あり」と誤検知する
    # （実機で2回連続フェッチしたHTMLを比較して確認済み）。
    (re.compile(r"sessionId=[a-f0-9]+"), "sessionId=X"),
]


def normalize_body_html_for_hash(html: str) -> str:
    for pattern, repl in VOLATILE_CONTENT_PATTERNS:
        html = pattern.sub(repl, html)
    return html


def body_hash(text: str) -> str:
    normalized = normalize_body_html_for_hash(text or "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    write_header = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["timestamp", "task", "post_id", "slug", "title", "result", "detail"]
        )
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


def log_row(task: str, post_id, slug: str, title: str, result: str, detail: str = "") -> dict:
    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "post_id": post_id or "",
        "slug": slug,
        "title": (title or "")[:80],
        "result": result,
        "detail": detail[:200],
    }


# ==================== WordPress REST APIヘルパー ====================

class WP:
    def __init__(self, site_url: str, username: str, app_password: str):
        self.site_url = site_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (username, app_password)
        self._category_cache: dict[str, int] = {}
        self._tag_cache: dict[str, int] = {}

    def find_post_by_slug(self, slug: str) -> dict | None:
        r = self.session.get(
            f"{self.site_url}/wp-json/wp/v2/posts",
            params={"slug": slug, "status": "publish,future,draft,pending,private,trash", "context": "edit"},
            timeout=30,
        )
        r.raise_for_status()
        results = r.json()
        return results[0] if results else None

    def get_post(self, post_id: int) -> dict:
        r = self.session.get(
            f"{self.site_url}/wp-json/wp/v2/posts/{post_id}",
            params={"context": "edit"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def get_or_create_category(self, name: str) -> int:
        if not name:
            return 1  # Uncategorized
        if name in self._category_cache:
            return self._category_cache[name]
        r = self.session.get(
            f"{self.site_url}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 100}, timeout=30,
        )
        r.raise_for_status()
        for c in r.json():
            if c["name"] == name:
                self._category_cache[name] = c["id"]
                return c["id"]
        r = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/categories", json={"name": name}, timeout=30,
        )
        r.raise_for_status()
        cat_id = r.json()["id"]
        self._category_cache[name] = cat_id
        return cat_id

    def get_or_create_tag(self, name: str) -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in self._tag_cache:
            return self._tag_cache[name]
        r = self.session.get(
            f"{self.site_url}/wp-json/wp/v2/tags",
            params={"search": name, "per_page": 100}, timeout=30,
        )
        r.raise_for_status()
        for t in r.json():
            if t["name"] == name:
                self._tag_cache[name] = t["id"]
                return t["id"]
        r = self.session.post(
            f"{self.site_url}/wp-json/wp/v2/tags", json={"name": name}, timeout=30,
        )
        if r.status_code not in (200, 201):
            return None
        tag_id = r.json()["id"]
        self._tag_cache[name] = tag_id
        return tag_id

    def create_post(self, payload: dict) -> dict:
        r = self.session.post(f"{self.site_url}/wp-json/wp/v2/posts", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()

    def update_post(self, post_id: int, payload: dict) -> dict:
        r = self.session.post(f"{self.site_url}/wp-json/wp/v2/posts/{post_id}", json=payload, timeout=60)
        r.raise_for_status()
        return r.json()

    def _post_media(self, image_bytes: bytes, content_type: str, filename: str) -> int | None:
        try:
            r = self.session.post(
                f"{self.site_url}/wp-json/wp/v2/media",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": content_type,
                },
                data=image_bytes,
                timeout=60,
            )
        except requests.RequestException:
            return None
        if r.status_code not in (200, 201):
            return None
        try:
            return r.json().get("id")
        except (ValueError, json.JSONDecodeError):
            return None

    def upload_media_from_url(self, image_url: str, filename: str) -> int | None:
        try:
            img_resp = requests.get(image_url, headers=EXBLOG_HEADERS, timeout=30)
            img_resp.raise_for_status()
        except requests.RequestException:
            return None
        content_type = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            return None

        # サーバー側WAF（SiteGuard Lite）は、note.com（assets.st-note.com）配信画像の
        # 元バイナリ（メタデータ等）をファイルアップロード時にしばしば誤検知してブロックする
        # （実機確認済み：同じ画像でもPillowで再エンコードするとブロックされなくなる）。
        # そのため常にPillowで再エンコードしたJPEGを優先的にアップロードし、
        # 何らかの理由で再エンコードできない画像（デコード不能等）に限り元データを使う。
        stem = Path(filename).stem or "thumb"
        try:
            from PIL import Image
            import io as _io

            img = Image.open(_io.BytesIO(img_resp.content))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            media_id = self._post_media(buf.getvalue(), "image/jpeg", f"{stem}.jpg")
            if media_id:
                return media_id
        except Exception:
            pass  # 再エンコードに失敗した場合は元データでフォールバックする

        return self._post_media(img_resp.content, content_type, filename)


# ==================== Codocブロック（本文埋め込み価格タグ）操作 ====================

def build_note_sync_meta(article: dict, full_title: str) -> dict:
    """
    note.com側の最新情報（価格・スキ数）をWordPress側のカスタムフィールドへ
    即時反映するためのmetaペイロードを組み立てる。

    【背景】一覧ページ・記事上部の価格バッジ（note-style-engagement.phpプラグイン）
    は、外部API（codoc.jp）への毎回のライブアクセスを避けるため、
    codoc_cached_price / codoc_cache_updated_at という「キャッシュ用」postmetaだけを
    読む設計になっている。このキャッシュはWP-Cronで1日2回しか更新されないため、
    新規投稿直後（初回のCron実行前）は codoc_cache_updated_at が空のままとなり、
    プラグイン側は別のフォールバック用メタキー codoc_price を参照する。しかし
    codoc_price はXML一括移行時にのみ設定される値で、auto_sync_blogs.py で
    新規作成した記事には一度も設定されたことが無かったため、Cronが回るまでの間
    「価格0円=無料」に見えてしまっていた（実機で確認済み）。
    codoc_price 自体はREST非公開（show_in_restが登録されていない）で直接
    書き込めないため、代わりに codoc_cached_price と codoc_cache_updated_at を
    投稿と同時に設定することで、Cronの実行を待たずに正しい価格を即時表示させる。
    """
    price = int(article.get("price") or 0)
    is_free = article.get("is_free", price == 0)
    return {
        "note_full_title": full_title,
        "codoc_cached_price": 0 if is_free else price,
        "codoc_cache_updated_at": int(time.time()),
        "nseb_like_count": int(article.get("like_count") or 0),
    }


def make_codoc_block(price: int) -> str:
    attrs = dict(CODOC_BLOCK_ATTRS_TEMPLATE)
    attrs["price"] = int(price)
    attrs_json = json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
    return (
        f"<!-- wp:codoc/codoc-block {attrs_json} -->\n"
        f'<div data-id="codoc-tag" class="codoc-entries"></div>\n'
        f"<!-- /wp:codoc/codoc-block -->"
    )


def extract_codoc_price(content: str) -> int | None:
    m = CODOC_BLOCK_PATTERN.search(content)
    if not m:
        return None
    try:
        attrs = json.loads(m.group(1))
        return int(attrs.get("price", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def replace_codoc_price(content: str, new_price: int) -> str | None:
    """本文中のCodocブロックのpriceだけを書き換える。ブロックが無ければNoneを返す。"""
    m = CODOC_BLOCK_PATTERN.search(content)
    if not m:
        return None
    try:
        attrs = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        return None
    attrs["price"] = int(new_price)
    new_block_attrs = json.dumps(attrs, separators=(",", ":"), ensure_ascii=False)
    new_block = (
        f"<!-- wp:codoc/codoc-block {new_block_attrs} -->\n"
        f'<div data-id="codoc-tag" class="codoc-entries"></div>\n'
        f"<!-- /wp:codoc/codoc-block -->"
    )
    return content[: m.start()] + new_block + content[m.end():]


def insert_codoc_paywall_simple(content: str, price: int) -> str:
    """
    新規note記事投稿時、可視文字数の25%地点相当にCodoc有料壁ブロックを挿入する。
    note側の本来の有料ライン位置（separator要素ID）が取得できればそちらを優先する
    （insert_codoc_paywall_at_element を参照）。
    """
    tag_or_code_pattern = re.compile(r"<[^>]+>|\[[^\]]+\]")
    visible_len = len(tag_or_code_pattern.sub("", content))
    if visible_len == 0:
        return content + f"\n\n{make_codoc_block(price)}\n\n"

    char_offset = max(int(visible_len * 0.25), 1)
    text_count = 0
    pos = 0
    target_idx = len(content)
    for match in tag_or_code_pattern.finditer(content):
        start, end = match.span()
        plain_text = content[pos:start]
        if text_count + len(plain_text) >= char_offset:
            target_idx = pos + (char_offset - text_count)
            break
        text_count += len(plain_text)
        pos = end
    after = content[target_idx:]
    close_m = re.search(r"</(?:p|div|figure|section|article|h[1-6]|blockquote)>|<br\s*/?>", after, re.IGNORECASE)
    insert_point = target_idx + close_m.end() if close_m else target_idx
    return content[:insert_point] + f"\n\n{make_codoc_block(price)}\n\n" + content[insert_point:]


def insert_codoc_paywall_at_element(content: str, element_id: str, price: int) -> str | None:
    """note API の separator（=有料ライン開始要素のid）が本文中に見つかれば、
    その要素の閉じタグ直後にCodocブロックを挿入する（最も正確な位置）。"""
    if not element_id:
        return None
    m = re.search(r'<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*\bid=["\']' + re.escape(element_id) + r'["\'][^>]*>', content)
    if not m:
        return None
    tag_name = m.group(1)
    close_m = re.search(r"</" + re.escape(tag_name) + r"\s*>", content[m.end():], re.IGNORECASE)
    insert_point = m.end() + (close_m.end() if close_m else 0)
    return content[:insert_point] + f"\n\n{make_codoc_block(price)}\n\n" + content[insert_point:]


SUBSCRIPTION_UPSELL_BOX_TEMPLATE = '''<div style="margin: 1.5em 0 2em 0; padding: 1.5em; border: 2px solid #ff7b7b; border-radius: 8px; background-color: #fff9f9;">
<p style="text-align: center; font-weight: bold; font-size: 1.1em; margin-bottom: 0.5em;">💡 この記事を単品で読みたい方へ</p>
<p style="text-align: center; margin-bottom: 1.5em;">単品でのご購入は、こちらの <a href="{source_url}" target="_blank" rel="noopener">【note元記事ページ】</a> からお手続きをお願いします。</p>
<p style="text-align: center; font-weight: bold; font-size: 1.2em; color: #d32f2f; margin-bottom: 0.5em;">🌟 【当サイト限定】圧倒的にお得なサブスク！ 🌟</p>
<p style="text-align: center; font-weight: bold; margin-bottom: 0;">月額メンバーシップにご登録いただくと、この記事はもちろん、<br /><span style="font-size: 1.2em; color: #d32f2f;">これから配信される最新記事も、過去の全記事も<br />すべて読み放題</span>になります！<br />単品で複数買うよりも断然お得です👇</p><div style="margin: 1em 0 0; text-align: center;"><div id="codoc-subscription-oEplngWcvQ" class="codoc-subscriptions"></div></div>
</div>
'''


def build_subscription_upsell_box(source_url: str) -> str:
    return SUBSCRIPTION_UPSELL_BOX_TEMPLATE.format(source_url=source_url)


FULL_TITLE_BOX_MARKER = "元記事確認用フルタイトル"

FULL_TITLE_BOX_TEMPLATE = '''<div style="margin: 0 0 2em 0; padding: 1.5em; border: 2px solid #4a90d9; border-radius: 8px; background-color: #eef6ff;">
<p style="text-align: center; font-weight: bold; font-size: 1.05em; margin-bottom: 0.5em; color: #2c5aa0;">📝 {marker}</p>
<p style="text-align: center; margin: 0; word-break: break-word;">{full_title}</p>
</div>
'''


def build_full_title_box(full_title: str) -> str:
    return FULL_TITLE_BOX_TEMPLATE.format(marker=FULL_TITLE_BOX_MARKER, full_title=html.escape(full_title))


def insert_full_title_box(content: str, full_title: str) -> str:
    """
    サブスク案内の赤枠の直後に、フルタイトルを表示する青枠を挿入する。
    既に挿入済み（FULL_TITLE_BOX_MARKERが本文中に存在する）場合は何もしない。
    赤枠が見つからない場合は本文の先頭に挿入する。
    """
    if not full_title:
        return content
    if FULL_TITLE_BOX_MARKER in content:
        return content

    marker = 'codoc-subscription-oEplngWcvQ" class="codoc-subscriptions"></div></div>'
    idx = content.find(marker)
    if idx == -1:
        insertion_point = 0
    else:
        close_idx = content.find("</div>", idx + len(marker))
        insertion_point = close_idx + len("</div>") if close_idx != -1 else idx + len(marker)

    return content[:insertion_point] + build_full_title_box(full_title) + content[insertion_point:]


def strip_toc_for_excerpt(html: str) -> str:
    """
    目次nav（<nav class="toc">）と、その直前にある「⬇️目次の読みたい項目を...⬇️」
    定型文の段落を取り除く。navが直接の兄弟要素ではなく別のタグに入れ子になっている
    場合もあるため、文書順でnavより前にある要素全体（find_previous）から探す。
    """
    if "<nav" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for nav in soup.find_all("nav"):
        boilerplate = nav.find_previous(string=re.compile("目次の読みたい項目"))
        if boilerplate is not None:
            container = boilerplate.find_parent(["p", "div", "li"]) or boilerplate
            container.decompose()
        nav.decompose()
        changed = True
    if not changed:
        return html
    return str(soup)


def make_excerpt_from_content(content: str, max_chars: int = 110) -> str:
    """
    一覧カードの概要（Cocoonテーマ）が本文冒頭の案内枠（サブスク定型文）や
    目次の定型文から始まってしまうのを防ぐため、案内枠・目次より後ろの
    本文から抜粋を生成する。
    Cocoonのカード概要はWordPress標準のget_the_excerptフィルタを経由せず
    post_excerpt（手動抜粋）をそのまま使うため、ここで直接生成して
    payloadのexcerptフィールドに設定する。
    """
    marker = 'codoc-subscription-oEplngWcvQ" class="codoc-subscriptions"></div></div>'
    idx = content.find(marker)
    if idx == -1:
        body = content
    else:
        close_idx = content.find("</div>", idx + len(marker))
        body = content[close_idx + len("</div>"):] if close_idx != -1 else content

    body = strip_toc_for_excerpt(body)

    text = re.sub(r"<[^>]+>", "", body)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + " […]"
    return text


def truncate_title_for_codoc(title: str) -> str:
    if not title or len(title) <= CODOC_TITLE_MAX_LENGTH:
        return title
    return title[: CODOC_TITLE_MAX_LENGTH - 3] + "..."


# ==================== 1. note 新着記事の自動投稿 ====================

HASH_TAG_PREFIX_CHARS = "#＃"


def normalize_note_tag_name(name: str) -> str:
    """
    note.comのハッシュタグ名（例: "#気象兵器"）をWordPressのタグ名として
    正規化する。先頭の#/＃と前後の空白を除去する。
    【2026-09-09追記】この正規化を欠いたまま note.com のハッシュタグ文字列を
    そのまま wp.get_or_create_tag() へ渡していたため、既存のシャープ無し
    タグ（例:「気象兵器」）と別タグ（「#気象兵器」）として分断・重複作成
    され、記事がタグ横断で分散する不具合があった（backfill_normalize_
    hashtag_tags.py で既存データを一括統合済み）。新規タグ取り込みは必ず
    ここを通すことで、以後同じ分断が再発しないようにする。
    """
    return name.strip().lstrip(HASH_TAG_PREFIX_CHARS).strip()


def fetch_note_article_list() -> list[dict]:
    """note.com公開APIから公開済み記事の一覧メタデータを取得する（ログイン不要）。"""
    articles = []
    page = 1
    while True:
        resp = requests.get(
            NOTE_CONTENTS_API_URL, params={"kind": "note", "page": page},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        for c in data["contents"]:
            if c.get("status") != "published":
                continue
            price_info = c.get("priceInfo") or {}
            articles.append({
                "key": c["key"],
                "title": c.get("name") or "",
                "price": c.get("price", 0),
                "publish_at": c.get("publishAt", ""),
                "thumbnail": c.get("eyecatch") or c.get("thumbnailExternalUrl") or "",
                "tags": list(dict.fromkeys(
                    normalize_note_tag_name(h["hashtag"]["name"])
                    for h in (c.get("hashtags") or []) if h.get("hashtag")
                    and normalize_note_tag_name(h["hashtag"]["name"])
                )),
                "paywall_element_id": c.get("separator"),
                "is_free": price_info.get("isFree", c.get("price", 0) == 0),
                "like_count": c.get("likeCount", 0),
                "url": f"https://note.com/{NOTE_USERNAME}/n/{c['key']}",
            })
        if data.get("isLastPage") or not data["contents"]:
            break
        page += 1
        time.sleep(0.3)
    return articles


def get_note_browser_page(pw, headless: bool):
    """note.com専用の保存済みログインセッション（chrome_user_data）でブラウザを開く。"""
    context = pw.chromium.launch_persistent_context(
        str(CHROME_USER_DATA_DIR),
        channel="chrome",
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport=None,
    )
    page = context.pages[0] if context.pages else context.new_page()
    return context, page


NOTE_TITLE_SELECTORS = [
    "h1.o-noteContentHeader__title",
    "h1[class*='ContentHeader__title']",
    "article h1",
    "h1",
]
NOTE_BODY_SELECTORS = [
    "div.note-common-styles__textnote-body",
    "div[data-name='body']",
    "div.p-article__content",
    "article",
]


def fetch_note_body_html(page, url: str) -> str:
    page.goto(url, wait_until="load", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    body_loc = None
    for sel in NOTE_BODY_SELECTORS:
        loc = page.locator(sel).first
        if loc.count() > 0:
            body_loc = loc
            break
    if body_loc is None:
        raise RuntimeError("本文コンテナが見つかりませんでした")
    try:
        body_loc.evaluate(
            "(el) => { el.querySelectorAll(\"[class*='paywall' i],[class*='Paywall']\").forEach(n => n.remove()); }"
        )
    except Exception:
        pass
    html = body_loc.inner_html()
    return rebuild_note_toc(html)


def rebuild_note_toc(html: str) -> str:
    """
    Playwrightでnote.comのレンダリング後DOMから直接取得した本文には、
    note.com側のVueコンポーネントの生マークアップ（<nav class="o-tableOfContents"
    data-v-...>）がそのまま含まれる。これはnote.com自身のJSランタイムが無いと
    クリックしても見出しへジャンプせず、初期状態で項目も表示されない
    （実機で確認済み: リンクが1件も生成されない）。
    link_converter.py の rebuild_toc() と同じロジックで、本文中の実際の
    h2/h3見出しから素のHTML（<nav class="toc"><ul class="toc-list">...）を
    組み立て直し、常に全項目が展開された状態でジャンプできる目次に置き換える。
    """
    if "o-tableOfContents" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    rebuild_toc(soup)
    return str(soup)


def sync_new_note_posts(
    wp: WP,
    articles: list[dict],
    state: dict,
    execute: bool,
    limit: int | None,
    link_maps: tuple[dict, dict],
) -> list[dict]:
    print("\n" + "=" * 60)
    print("【1】note 新着記事の自動投稿")
    print("=" * 60)

    targets = []
    for a in articles:
        slug = a["key"]  # note.comのkeyは既に"n"始まり（例: n8a33d850cd3c）なのでそのままスラッグに使う
        if wp.find_post_by_slug(slug) is None:
            targets.append(a)
    print(f"note公開済み記事: {len(articles)}件 / WordPress未投稿: {len(targets)}件")

    if limit:
        targets = targets[:limit]

    log_rows = []
    if not targets:
        return log_rows

    if not execute:
        for a in targets:
            print(f"  [検出] {a['title'][:50]}  ({a['key']})")
            log_rows.append(log_row("new_note", None, a["key"], a["title"], "dry_run_would_create"))
        return log_rows

    from playwright.sync_api import sync_playwright
    # 2026-08-25 追記：新規note記事のCodocサーバー側「購読プラン紐付け」を
    # 自動化するため、backfill_codoc_subscription_linkage.py の実装を再利用する
    # （詳細はそのファイルのdocstring・下記の呼び出し箇所のコメント参照）。
    from backfill_codoc_subscription_linkage import process_entry as link_codoc_subscription

    with sync_playwright() as pw:
        context, page = get_note_browser_page(pw, headless=True)
        try:
            for i, a in enumerate(targets, start=1):
                slug = a["key"]
                print(f"[{i}/{len(targets)}] {a['title'][:50]}")
                try:
                    body_html = fetch_note_body_html(page, a["url"])
                    title = truncate_title_for_codoc(a["title"] or "無題")

                    content = body_html
                    if a["price"] and not a["is_free"]:
                        with_marker = insert_codoc_paywall_at_element(content, a.get("paywall_element_id"), a["price"])
                        content = with_marker if with_marker is not None else insert_codoc_paywall_simple(content, a["price"])

                    # 記事冒頭に「単品購入 or 月額サブスク」の案内枠（サブスク登録ボタン込み）を付与する。
                    # generate_perfect_wp_xml.py での過去の一括移行と同じ形式・位置（本文の最上部）。
                    content = build_subscription_upsell_box(a["url"]) + content
                    # 赤枠の直後に、note側の完全なタイトルを示す青枠を付与する。
                    content = insert_full_title_box(content, a["title"])
                    # 本文中のnote/exblog過去記事リンクをサイト内リンクに変換する（赤枠は対象外）。
                    note_map, exblog_map = link_maps
                    content, _ = convert_internal_links(content, note_map, exblog_map)
                    error_markers = find_error_text_markers(content)
                    if error_markers:
                        raise RuntimeError(f"本文にサーバーエラー文字列が混入: {error_markers}")

                    full_title = a["title"] or title

                    payload = {
                        "title": title,
                        "slug": slug,
                        "content": content,
                        "excerpt": full_title,
                        "meta": build_note_sync_meta(a, full_title),
                        "status": "publish",
                        "date": a["publish_at"][:19] if a["publish_at"] else None,
                        # 「note」カテゴリー（固定ID）を必ず割り当てる。サイドバーの
                        # 絞り込み検索（custom-search-filter.php）はカテゴリーIDでしか
                        # 判定できないため、Uncategoriedのままだと検索結果から漏れる
                        # （2026-08-11 実機で確認・修正）。
                        "categories": [NOTE_CATEGORY_ID],
                    }
                    payload = {k: v for k, v in payload.items() if v is not None}

                    tag_ids = [wp.get_or_create_tag(t) for t in a["tags"]]
                    tag_ids = [t for t in tag_ids if t]
                    if tag_ids:
                        payload["tags"] = tag_ids

                    created = wp.create_post(payload)
                    post_id = created["id"]

                    # フォールバック：REST側の一時的な不整合等でカテゴリーが実際には
                    # 反映されていなかった場合、Uncategorizedのまま公開され続けることを
                    # 防ぐため、レスポンスを検証し必要なら明示的に再設定する。
                    if NOTE_CATEGORY_ID not in (created.get("categories") or []):
                        try:
                            wp.update_post(post_id, {"categories": [NOTE_CATEGORY_ID]})
                            print(f"    [フォールバック] post_id={post_id} のnoteカテゴリーを再設定しました")
                            log_rows.append(
                                log_row("new_note", post_id, slug, a["title"], "category_fallback_applied")
                            )
                        except requests.RequestException as e:
                            print(f"    [警告] post_id={post_id} のnoteカテゴリー再設定に失敗: {e}")
                            log_rows.append(
                                log_row("new_note", post_id, slug, a["title"], "category_fallback_failed", str(e))
                            )

                    # Codocサーバー側の「購読プラン紐付け」（2026-08-25追記）。
                    # WordPress投稿内のCodocブロックJSON属性（"subscriptions":{"4133":true}）
                    # は、REST API経由の新規作成・再保存のどちらでもCodocサーバー側の
                    # 実際の紐付けレコードには反映されないことを実機で確認済み
                    # （backfill_codoc_subscription_linkage.py のdocstring参照）。
                    # 有料記事のみ、Codocダッシュボードの個別記事編集ページを開いて
                    # チェックボックスを明示的にONにしないと、サブスク会員であっても
                    # ペイウォールが解除されない。
                    if a["price"] and not a["is_free"]:
                        entry_code = (created.get("meta") or {}).get("codoc_entry_code")
                        if not entry_code:
                            # ごく稀にCodoc側のエントリー作成がREST応答に間に合わない
                            # 場合に備え、短い間隔で数回だけ再確認する
                            # （実機では作成直後の応答に既に含まれることを確認済み）。
                            for _ in range(3):
                                time.sleep(2)
                                fresh = wp.get_post(post_id)
                                entry_code = (fresh.get("meta") or {}).get("codoc_entry_code")
                                if entry_code:
                                    break
                        if entry_code:
                            try:
                                link_result = link_codoc_subscription(page, entry_code, True)
                                print(f"    [Codoc紐付け] entry_code={entry_code} -> {link_result['status']}")
                                log_rows.append(
                                    log_row("new_note", post_id, slug, a["title"], f"codoc_link_{link_result['status']}")
                                )
                            except Exception as e:
                                print(f"    [警告] Codoc購読プラン紐付けに失敗: {e}")
                                log_rows.append(
                                    log_row("new_note", post_id, slug, a["title"], "codoc_link_error", str(e))
                                )
                        else:
                            print(f"    [警告] post_id={post_id} のcodoc_entry_codeが取得できず、"
                                  "購読プラン紐付けをスキップしました"
                                  "（後日 backfill_codoc_subscription_linkage.py で補完可能）")
                            log_rows.append(
                                log_row("new_note", post_id, slug, a["title"], "codoc_link_skipped_no_entry_code")
                            )

                    if a["thumbnail"]:
                        media_id = wp.upload_media_from_url(a["thumbnail"], f"{a['key']}-thumb.jpg")
                        if media_id:
                            wp.update_post(post_id, {"featured_media": media_id})

                    new_hash = body_hash(body_html)
                    state.setdefault("note", {})[a["key"]] = {"body_hash": new_hash, "price": a["price"]}

                    print(f"    [OK] post_id={post_id}")
                    log_rows.append(log_row("new_note", post_id, slug, title, "success", f"body_hash={new_hash[:12]}"))
                except Exception as e:
                    print(f"    [失敗] {e}")
                    log_rows.append(log_row("new_note", None, slug, a["title"], "failed", str(e)))
                time.sleep(REQUEST_DELAY_SECONDS)
        finally:
            context.close()

    return log_rows


# ==================== 2. エキサイトブログ新着記事の自動投稿 ====================

def exblog_polite_get(url: str) -> str:
    resp = requests.get(url, headers=EXBLOG_HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    time.sleep(random.uniform(1.0, 2.0))
    return resp.text


def fetch_exblog_recent_article_urls(max_months: int = 2) -> list[str]:
    """直近の月別アーカイブだけを見る（新着検知が目的のため全期間走査は不要）。"""
    html = exblog_polite_get(EXBLOG_BASE_URL + "/")
    idx = html.find("以前の記事")
    if idx == -1:
        return []
    end = html.find("</table>", idx)
    section = html[idx: end + 10]
    months = sorted(set(re.findall(r'href="(https://rakusen\.exblog\.jp/m[\d-]+/)"', section)), reverse=True)
    months = months[:max_months]

    urls: list[str] = []
    seen: set[str] = set()
    for month_url in months:
        html = exblog_polite_get(month_url)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a.archivelist_thumb-link"):
            href = a.get("href")
            if href:
                full = urljoin(EXBLOG_BASE_URL, href)
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
    return urls


def parse_exblog_article(url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    post = soup.select_one("div.POST")
    if not post:
        return None
    title_el = post.select_one("div.POST_HEAD h3")
    title = title_el.get_text(strip=True) if title_el else ""
    body_el = post.select_one("div.POST_BODY")
    body_html = body_el.decode_contents().strip() if body_el else ""
    tail = post.select_one(".POST_TAIL")
    published_at, category = "", ""
    if tail:
        for a in tail.find_all("a"):
            href = a.get("href") or ""
            text = a.get_text(strip=True)
            if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", text):
                published_at = text
            elif re.search(r"/i\d+/?$", href):
                category = text
    return {"url": url, "title": title, "published_at": published_at, "category": category, "body_html": body_html}


def sync_new_exblog_posts(wp: WP, execute: bool, limit: int | None, link_maps: tuple[dict, dict]) -> list[dict]:
    print("\n" + "=" * 60)
    print("【2】エキサイトブログ新着記事の自動投稿")
    print("=" * 60)

    article_urls = fetch_exblog_recent_article_urls()
    print(f"直近アーカイブ内の記事: {len(article_urls)}件")

    targets = []
    for url in article_urls:
        m = re.search(r"/(\d+)/?$", url)
        article_id = m.group(1) if m else None
        if not article_id:
            continue
        slug = f"exblog-{article_id}"
        if wp.find_post_by_slug(slug) is None:
            targets.append((article_id, url))
    print(f"WordPress未投稿: {len(targets)}件")

    if limit:
        targets = targets[:limit]

    log_rows = []
    for i, (article_id, url) in enumerate(targets, start=1):
        slug = f"exblog-{article_id}"
        print(f"[{i}/{len(targets)}] {url}")
        if not execute:
            log_rows.append(log_row("new_exblog", None, slug, url, "dry_run_would_create"))
            continue
        try:
            html = exblog_polite_get(url)
            data = parse_exblog_article(url, html)
            if not data or not data["title"]:
                log_rows.append(log_row("new_exblog", None, slug, url, "parse_failed"))
                continue

            categories = [EXBLOG_CATEGORY_ID]
            if data["category"]:
                categories.append(wp.get_or_create_category(data["category"]))

            note_map, exblog_map = link_maps
            body_html, _ = convert_internal_links(data["body_html"], note_map, exblog_map)
            error_markers = find_error_text_markers(body_html)
            if error_markers:
                log_rows.append(log_row("new_exblog", None, slug, url, "aborted_error_text_detected", ",".join(error_markers)))
                continue

            payload = {
                "title": data["title"],
                "slug": slug,
                "content": body_html,
                "status": "publish",
                "categories": categories,
            }
            if data["published_at"]:
                try:
                    dt = datetime.strptime(data["published_at"], "%Y-%m-%d %H:%M")
                    payload["date"] = dt.strftime("%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    pass

            created = wp.create_post(payload)
            print(f"    [OK] post_id={created['id']}")
            log_rows.append(log_row("new_exblog", created["id"], slug, data["title"], "success"))
        except Exception as e:
            print(f"    [失敗] {e}")
            log_rows.append(log_row("new_exblog", None, slug, url, "failed", str(e)))
        time.sleep(REQUEST_DELAY_SECONDS)

    return log_rows


# ==================== 3. note記事の更新追従（30日以内） ====================

def sync_note_updates(
    wp: WP,
    articles: list[dict],
    state: dict,
    execute: bool,
    limit: int | None,
    link_maps: tuple[dict, dict],
    force_keys: set[str] | None = None,
) -> list[dict]:
    print("\n" + "=" * 60)
    print(f"【3】note記事の更新追従（公開から{NOTE_UPDATE_WINDOW_DAYS}日以内）")
    print("=" * 60)

    force_keys = force_keys or set()

    now = datetime.now()
    recent = []
    for a in articles:
        if a["key"] in force_keys:
            # 【2026-09-09追記：即時更新の強制指定】--key で明示された記事は、
            # 30日ウィンドウの対象外（古い記事の後追い修正等）であっても必ず含める。
            recent.append(a)
            continue
        if not a["publish_at"]:
            continue
        try:
            pub_dt = datetime.strptime(a["publish_at"][:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if (now - pub_dt).days <= NOTE_UPDATE_WINDOW_DAYS:
            recent.append(a)
    print(f"公開から{NOTE_UPDATE_WINDOW_DAYS}日以内のnote記事: {len(recent)}件"
          + (f"（うち強制指定: {len(force_keys)}件）" if force_keys else ""))

    if limit:
        recent = recent[:limit]

    log_rows = []
    if not recent:
        return log_rows

    note_state = state.setdefault("note", {})

    from playwright.sync_api import sync_playwright
    # 【2026-09-01追記】content更新でCodoc側の購読プラン紐付けが解除される副作用
    # への対策（backfill_internal_links.py のdocstring・CLAUDE.md参照）。
    # update-noteは本文を書き換える経路のため、更新のたびに再確認・再設定する。
    from backfill_codoc_subscription_linkage import process_entry as relink_codoc_subscription

    with sync_playwright() as pw:
        context, page = get_note_browser_page(pw, headless=True)
        try:
            for i, a in enumerate(recent, start=1):
                key = a["key"]
                slug = key  # note.comのkeyは既に"n"始まりなのでそのままスラッグに使う
                post = wp.find_post_by_slug(slug)
                if post is None:
                    continue  # まだWP側に存在しない（【1】の対象。ここでは何もしない）

                print(f"[{i}/{len(recent)}] {a['title'][:50]}")
                try:
                    body_html = fetch_note_body_html(page, a["url"])
                    # 【2026-09-09追記：タイトル・タグのみの変更を見逃していた不具合の修正】
                    # 従来は本文HTMLのハッシュだけで変更を検知していたため、note.com側で
                    # タイトルやハッシュタグ（タグ）だけを編集し本文自体は変えていない
                    # ケース（例: nac32945bc0e8 のタイトルへの「【気象兵器】」追記）が
                    #「変更なし」判定されてしまい、note_full_title・タグがWordPress側へ
                    # 一切反映されないという不具合があった（検索結果のタイトル一致優先
                    # ソート・サイドバーのタグ一覧の双方に影響していた）。タイトル・タグも
                    # ハッシュの対象に含める。
                    change_signature = (
                        (a["title"] or "") + "\n"
                        + ",".join(sorted(a.get("tags") or [])) + "\n"
                        + body_html
                    )
                    new_hash = body_hash(change_signature)
                    old_hash = note_state.get(key, {}).get("content_hash")
                    forced = key in force_keys

                    if old_hash == new_hash and not forced:
                        print("    変更なし")
                        time.sleep(REQUEST_DELAY_SECONDS)
                        continue

                    print("    [強制更新]" if forced and old_hash == new_hash else "    [変更を検出]")
                    if not execute:
                        log_rows.append(log_row("update_note", post["id"], slug, a["title"], "dry_run_would_update"))
                        time.sleep(REQUEST_DELAY_SECONDS)
                        continue

                    # 既存記事のCodoc価格は変更せず維持したまま本文だけ差し替える
                    existing_content = post.get("content", {}).get("raw", "") or ""
                    existing_price = extract_codoc_price(existing_content)

                    content = body_html
                    if existing_price and existing_price > 0:
                        with_marker = insert_codoc_paywall_at_element(content, a.get("paywall_element_id"), existing_price)
                        content = with_marker if with_marker is not None else insert_codoc_paywall_simple(content, existing_price)

                    # 本文差し替え時に案内枠（サブスク登録ボタン込み）が消えないよう、
                    # 新規投稿時と同じ形式で必ず付け直す。
                    content = build_subscription_upsell_box(a["url"]) + content
                    content = insert_full_title_box(content, a["title"])
                    note_map, exblog_map = link_maps
                    content, _ = convert_internal_links(content, note_map, exblog_map)
                    error_markers = find_error_text_markers(content)
                    if error_markers:
                        raise RuntimeError(f"本文にサーバーエラー文字列が混入: {error_markers}")

                    full_title = a["title"] or post.get("title", {}).get("raw", "")

                    # フォールバック：過去のバグ等でこの記事のnoteカテゴリーが
                    # Uncategorizedのまま残っていた場合、通常の更新のついでに
                    # 自己修復する（Uncategorizedが検索結果から漏れ続ける事故を防ぐ）。
                    update_payload = {
                        "content": content,
                        "excerpt": full_title,
                        "meta": {
                            "note_full_title": full_title,
                            "codoc_cached_price": existing_price or 0,
                            "codoc_cache_updated_at": int(time.time()),
                            "nseb_like_count": int(a.get("like_count") or 0),
                        },
                    }
                    existing_categories = post.get("categories") or []
                    if NOTE_CATEGORY_ID not in existing_categories:
                        update_payload["categories"] = sorted(set(existing_categories) | {NOTE_CATEGORY_ID} - {1})
                        print(f"    [フォールバック] post_id={post['id']} のnoteカテゴリーを自己修復します")

                    # 【2026-09-09追記】新規投稿時（sync_new_note_posts）と同様、更新時にも
                    # note.com側の最新タグを必ず同期する。従来はここでタグを一切
                    # 更新していなかったため、記事公開後に追加・変更されたハッシュタグが
                    # 「キーワードから探す」一覧へ永遠に反映されない不具合があった。
                    tag_ids = [wp.get_or_create_tag(t) for t in (a.get("tags") or [])]
                    tag_ids = [t for t in tag_ids if t]
                    update_payload["tags"] = tag_ids

                    wp.update_post(post["id"], update_payload)
                    note_state[key] = {"content_hash": new_hash, "price": existing_price}
                    print(f"    [OK] post_id={post['id']} を更新しました")
                    log_rows.append(log_row("update_note", post["id"], slug, a["title"], "success"))

                    entry_code = (post.get("meta") or {}).get("codoc_entry_code")
                    if not entry_code:
                        fresh = wp.get_post(post["id"])
                        entry_code = (fresh.get("meta") or {}).get("codoc_entry_code")
                    if entry_code:
                        try:
                            relink_result = relink_codoc_subscription(page, entry_code, True)
                            print(f"    [Codoc再紐付け] entry_code={entry_code} -> {relink_result['status']}")
                            log_rows.append(
                                log_row("update_note", post["id"], slug, a["title"], f"codoc_relink_{relink_result['status']}")
                            )
                        except Exception as e:
                            print(f"    [警告] Codoc再紐付けに失敗: {e}")
                            log_rows.append(log_row("update_note", post["id"], slug, a["title"], "codoc_relink_error", str(e)))
                except Exception as e:
                    print(f"    [失敗] {e}")
                    log_rows.append(log_row("update_note", post["id"], slug, a["title"], "failed", str(e)))
                time.sleep(REQUEST_DELAY_SECONDS)
        finally:
            context.close()

    return log_rows


# ==================== 4. Codoc自動値下げ（90日経過） ====================

def fetch_all_wp_posts_with_dates(wp: WP) -> list[dict]:
    posts = []
    page = 1
    while True:
        r = wp.session.get(
            f"{wp.site_url}/wp-json/wp/v2/posts",
            params={
                "per_page": 100, "page": page,
                "status": "publish,future,draft,pending,private",
                "context": "edit", "_fields": "id,slug,date,title",
            },
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return posts


def sync_codoc_discount(wp: WP, execute: bool, limit: int | None) -> list[dict]:
    print("\n" + "=" * 60)
    print(f"【4】Codoc自動値下げ（投稿から{CODOC_DISCOUNT_AFTER_DAYS}日以上経過 かつ 価格>{CODOC_DISCOUNT_PRICE}円）")
    print("=" * 60)

    posts = fetch_all_wp_posts_with_dates(wp)
    now = datetime.now()
    cutoff = now - timedelta(days=CODOC_DISCOUNT_AFTER_DAYS)

    old_posts = []
    for p in posts:
        try:
            post_dt = datetime.strptime(p["date"][:19], "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
        if post_dt <= cutoff:
            old_posts.append(p)
    print(f"全記事: {len(posts)}件 / 投稿から{CODOC_DISCOUNT_AFTER_DAYS}日以上経過: {len(old_posts)}件（Codocブロックの有無はこれから個別確認）")

    if limit:
        old_posts = old_posts[:limit]

    log_rows = []
    for i, p in enumerate(old_posts, start=1):
        title = p["title"]["raw"] if isinstance(p.get("title"), dict) else str(p.get("title", ""))
        full = wp.get_post(p["id"])
        content = full.get("content", {}).get("raw", "") or ""
        price = extract_codoc_price(content)
        if price is None or price <= CODOC_DISCOUNT_PRICE:
            continue

        print(f"[{i}/{len(old_posts)}] id={p['id']} price={price}円 -> {CODOC_DISCOUNT_PRICE}円  {title[:40]}")
        if not execute:
            log_rows.append(log_row("discount_codoc", p["id"], p["slug"], title, "dry_run_would_discount", f"{price}->{CODOC_DISCOUNT_PRICE}"))
            continue

        new_content = replace_codoc_price(content, CODOC_DISCOUNT_PRICE)
        if new_content is None:
            log_rows.append(log_row("discount_codoc", p["id"], p["slug"], title, "failed", "codoc block parse error"))
            continue
        try:
            # 値下げ前の元価格を codoc_price_before_discount へ保存する。
            # アーカイブ割引ボックス（note-style-engagement.php の
            # prepend_archive_discount_box）が「note定価◯円から」の比較文言を
            # 表示する唯一のデータソースになるため、値下げと同時に必ず設定する。
            # 併せて codoc_cached_price / codoc_cache_updated_at も同時に書き換える
            # （[[build_note_sync_meta と同じ理由]]。WP-Cronの次回実行（最大1時間後）
            # を待つと、値下げ済みなのに割引ボックスが一時的に表示されない
            # 空白期間ができてしまうため、Cronを待たず即時反映させる）。
            wp.update_post(p["id"], {
                "content": new_content,
                "meta": {
                    "codoc_price_before_discount": price,
                    "codoc_cached_price": CODOC_DISCOUNT_PRICE,
                    "codoc_cache_updated_at": int(time.time()),
                },
            })
            print("    [OK] 値下げしました")
            log_rows.append(log_row("discount_codoc", p["id"], p["slug"], title, "success", f"{price}->{CODOC_DISCOUNT_PRICE}"))
        except requests.RequestException as e:
            print(f"    [失敗] {e}")
            log_rows.append(log_row("discount_codoc", p["id"], p["slug"], title, "failed", str(e)))
        time.sleep(REQUEST_DELAY_SECONDS)

    return log_rows


# ==================== メイン ====================

def main() -> None:
    parser = argparse.ArgumentParser(description="note / エキサイトブログ とWordPressの自動同期")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument(
        "--only",
        choices=["new-note", "new-exblog", "update-note", "discount-codoc"],
        default=None,
        help="指定した処理だけを実行する（省略時は全処理を実行）",
    )
    parser.add_argument("--limit", type=int, default=None, help="各処理の対象件数の上限（試験実行用）")
    parser.add_argument(
        "--key",
        action="append",
        default=None,
        help="指定したnote記事キー（例: nac32945bc0e8）を、公開30日超・本文ハッシュ不変でも"
             "強制的に最新内容へ即時上書き更新する（複数指定可）。--only update-note と併用も可。",
    )
    args = parser.parse_args()

    creds = load_credentials()
    wp = WP(creds["site_url"], creds["username"], creds["application_password"])
    state = load_state()

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print(f"実行モード: {mode}")
    if args.only:
        print(f"実行対象: {args.only} のみ")

    all_log_rows: list[dict] = []

    need_link_maps = args.only in (None, "new-note", "new-exblog", "update-note") or bool(args.key)
    link_maps = fetch_link_maps(wp) if need_link_maps else ({}, {})
    if need_link_maps:
        print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

    need_note_list = args.only in (None, "new-note", "update-note") or bool(args.key)
    note_articles = fetch_note_article_list() if need_note_list else []
    if need_note_list:
        print(f"\nnote公開済み記事一覧を取得しました: {len(note_articles)}件")

    if args.only in (None, "new-note"):
        all_log_rows += sync_new_note_posts(wp, note_articles, state, args.execute, args.limit, link_maps)
        save_state(state)

    if args.only in (None, "new-exblog"):
        all_log_rows += sync_new_exblog_posts(wp, args.execute, args.limit, link_maps)

    if args.only in (None, "update-note") or args.key:
        all_log_rows += sync_note_updates(
            wp, note_articles, state, args.execute, args.limit, link_maps,
            force_keys=set(args.key) if args.key else None,
        )
        save_state(state)

    if args.only in (None, "discount-codoc"):
        all_log_rows += sync_codoc_discount(wp, args.execute, args.limit)

    # 【2026-09-02追記：横断的な安全網】content更新に限らずcategories等の更新
    # だけでもCodoc側の購読プラン紐付けが解除される副作用が確認されている
    # （backfill_internal_links.py・categorize_articles_ai.py のdocstring参照）。
    # 個別関数ごとに再紐付け処理を書き込む方式は書き漏れのリスクが常に残るため、
    # ここで today 実際に post_id が更新された投稿（success系の結果を持つ行）を
    # 横断的に集約し、Codoc有料記事であれば最後に一括で再紐付けを確認する。
    # sync_new_note_posts / sync_note_updates は既に個別に再紐付け済みだが、
    # 二重実行しても process_entry は冪等（既にlinked済みならスキップ）なので
    # 無害。sync_codoc_discount 等、個別対応していない経路の取りこぼしを
    # ここで一括して拾う。
    if args.execute:
        touched_ids = sorted({
            row["post_id"] for row in all_log_rows
            if row.get("post_id") and row.get("result") == "success"
        })
        if touched_ids:
            print("\n" + "=" * 60)
            print(f"【5】Codoc購読プラン紐付けの横断チェック（本日更新した{len(touched_ids)}件）")
            print("=" * 60)
            from backfill_codoc_subscription_linkage import process_entry as relink_codoc_subscription
            from playwright.sync_api import sync_playwright as _sync_playwright
            with _sync_playwright() as pw:
                context, page = get_note_browser_page(pw, headless=True)
                try:
                    for pid in touched_ids:
                        try:
                            fresh = wp.get_post(pid)
                            entry_code = (fresh.get("meta") or {}).get("codoc_entry_code")
                        except Exception as e:
                            print(f"  id={pid}: [警告] 取得失敗 {e}")
                            continue
                        if not entry_code:
                            continue
                        try:
                            result = relink_codoc_subscription(page, entry_code, True)
                            print(f"  id={pid} entry_code={entry_code} -> {result['status']}")
                        except Exception as e:
                            print(f"  id={pid} entry_code={entry_code}: [警告] 再紐付け失敗 {e}")
                finally:
                    context.close()

    # 【2026-09-09追記：サイドバー「キーワードから探す（50音順）」の自動最新化】
    # note記事の新規投稿・更新追従（タグ同期を含む）でWordPressのタグ
    # （post_tag）が増減しうるのは new-note と update-note の2経路のみ。
    # 従来はタグ一覧固定ページ（/tags/、generate_tag_index.py）の再生成が
    # 手動運用のままだったため、新しいキーワードのタグを記事に付けても
    # サイドバーの一覧に反映されるまで誰かが手動でスクリプトを実行するまで
    # 気づかれない状態だった。ここで毎回の本番実行の最後に自動で再生成・
    # 固定ページへ反映することで、以後は同期のたびに必ず追従する。
    if args.execute and (args.only in (None, "new-note", "update-note") or args.key):
        print("\n" + "=" * 60)
        print("【6】サイドバー「キーワードから探す」タグ一覧ページの自動再生成")
        print("=" * 60)
        try:
            import generate_tag_index as tag_index_mod
            tags = tag_index_mod.fetch_all_tags(wp.site_url, (creds["username"], creds["application_password"]))
            print(f"使用中のタグ: {len(tags)}件")
            kks = tag_index_mod.pykakasi.kakasi()
            page_html = tag_index_mod.build_html(tags, kks)
            tag_index_mod.OUTPUT_PATH.write_text(page_html, encoding="utf-8")
            tag_wp = tag_index_mod.WP(creds["site_url"], creds["username"], creds["application_password"])
            existing = tag_wp.find_page_by_slug(tag_index_mod.PAGE_SLUG)
            if existing:
                tag_wp.update_page_content(existing["id"], page_html)
                print(f"  [OK] タグ一覧ページを更新しました: id={existing['id']} slug={tag_index_mod.PAGE_SLUG}")
            else:
                created = tag_wp.create_page(tag_index_mod.PAGE_TITLE, tag_index_mod.PAGE_SLUG, page_html)
                print(f"  [OK] タグ一覧ページを新規作成しました: id={created['id']} link={created.get('link')}")
        except Exception as e:
            print(f"  [警告] タグ一覧ページの自動更新に失敗しました: {e}")

    append_log(all_log_rows)

    print("\n" + "=" * 60)
    counts: dict[str, int] = {}
    for row in all_log_rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    if counts:
        for result, count in counts.items():
            print(f"  {result}: {count}件")
    else:
        print("  対象記事はありませんでした。")
    print(f"ログを保存しました: {LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
