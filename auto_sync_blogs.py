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
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup

# Windows既定のコンソールエンコーディング（cp932）では記事タイトル中の絵文字が
# 表示できずクラッシュする（実機で確認済み）。タスクスケジューラでの無人実行が
# 途中で落ちないよう、標準出力をUTF-8に固定する。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from link_converter import is_note_toc_nav, rebuild_toc
from internal_article_links import (
    NOTE_CATEGORY_ID,
    SITE_DOMAIN,
    build_blogcard_html,
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

# 【2026-09-13追記：ReadTimeoutによる月次メンテナンス停止事故を受けて】
# WordPress REST APIへの通信は、サーバー負荷やネットワークの一時的な不調で
# 30秒では読み切れずタイムアウトすることがある（wp.get_post等）。
# タイムアウトを延長した上で、通信エラー時は指数バックオフで自動リトライする。
WP_REQUEST_TIMEOUT = 60          # 通常のGET/POST用
WP_REQUEST_TIMEOUT_HEAVY = 90    # 本文全体を送るcreate_post/update_post用
WP_MAX_RETRIES = 3
WP_RETRY_BACKOFF_BASE_SECONDS = 3  # 1回目失敗後3秒、2回目6秒、3回目12秒待機


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

    def _request(self, method: str, url: str, raise_for_status: bool = True, **kwargs) -> requests.Response:
        """通信エラー（タイムアウト・接続断等）時に指数バックオフで最大
        WP_MAX_RETRIES 回まで自動リトライする共通ヘルパー。全WP REST API呼び出しは
        これを経由する（2026-09-13追記：ReadTimeoutで月次メンテナンスが停止した
        事故を受けて導入）。raise_for_status=False を指定すると、レスポンスが
        返ってきた場合（4xx/5xx含む）はリトライも例外送出もせずそのまま返す
        （get_or_create_tag のタグ作成時、409/422等の業務エラーを正常系として
        呼び出し元で判定する既存の挙動を壊さないため）。リトライの対象は
        あくまで requests.exceptions.RequestException（タイムアウト・接続エラー等の
        通信そのものの失敗）のみ。
        """
        kwargs.setdefault("timeout", WP_REQUEST_TIMEOUT)
        last_exc: requests.exceptions.RequestException | None = None
        for attempt in range(1, WP_MAX_RETRIES + 1):
            try:
                r = self.session.request(method, url, **kwargs)
                if raise_for_status:
                    r.raise_for_status()
                return r
            except requests.exceptions.RequestException as e:
                last_exc = e
                if attempt >= WP_MAX_RETRIES:
                    break
                wait = WP_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(f"    [リトライ {attempt}/{WP_MAX_RETRIES}] {method} {url} が失敗（{type(e).__name__}: {e}）。{wait}秒待機して再試行します")
                time.sleep(wait)
        assert last_exc is not None
        raise last_exc

    def find_post_by_slug(self, slug: str) -> dict | None:
        r = self._request(
            "GET", f"{self.site_url}/wp-json/wp/v2/posts",
            params={"slug": slug, "status": "publish,future,draft,pending,private,trash", "context": "edit"},
        )
        results = r.json()
        return results[0] if results else None

    def get_post(self, post_id: int) -> dict:
        r = self._request(
            "GET", f"{self.site_url}/wp-json/wp/v2/posts/{post_id}",
            params={"context": "edit"},
        )
        return r.json()

    def get_or_create_category(self, name: str) -> int:
        if not name:
            return 1  # Uncategorized
        if name in self._category_cache:
            return self._category_cache[name]
        r = self._request(
            "GET", f"{self.site_url}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 100},
        )
        for c in r.json():
            if c["name"] == name:
                self._category_cache[name] = c["id"]
                return c["id"]
        r = self._request("POST", f"{self.site_url}/wp-json/wp/v2/categories", json={"name": name})
        cat_id = r.json()["id"]
        self._category_cache[name] = cat_id
        return cat_id

    def get_or_create_tag(self, name: str) -> int | None:
        name = (name or "").strip()
        if not name:
            return None
        if name in self._tag_cache:
            return self._tag_cache[name]
        r = self._request(
            "GET", f"{self.site_url}/wp-json/wp/v2/tags",
            params={"search": name, "per_page": 100},
        )
        for t in r.json():
            if t["name"] == name:
                self._tag_cache[name] = t["id"]
                return t["id"]
        r = self._request(
            "POST", f"{self.site_url}/wp-json/wp/v2/tags", json={"name": name},
            raise_for_status=False,
        )
        if r.status_code not in (200, 201):
            return None
        tag_id = r.json()["id"]
        self._tag_cache[name] = tag_id
        return tag_id

    def create_post(self, payload: dict) -> dict:
        r = self._request(
            "POST", f"{self.site_url}/wp-json/wp/v2/posts", json=payload,
            timeout=WP_REQUEST_TIMEOUT_HEAVY,
        )
        return r.json()

    def update_post(self, post_id: int, payload: dict) -> dict:
        r = self._request(
            "POST", f"{self.site_url}/wp-json/wp/v2/posts/{post_id}", json=payload,
            timeout=WP_REQUEST_TIMEOUT_HEAVY,
        )
        return r.json()

    def delete_replaced_featured_media(self, old_media_id: int, new_media_id: int, note_key: str) -> str:
        """
        アイキャッチを差し替えた後、不要になった旧画像をメディアライブラリから完全削除する
        （2026-10-18追記）。誤削除を防ぐため、以下をすべて満たす場合のみ削除する。
        (1) 自動同期がこの記事用にアップロードした画像（ファイル名が "{note_key}-thumb" 始まり）。
            手動で設定した画像には触れない。
        (2) どの投稿・固定ページの本文からも参照されていない。内部リンクカード
            （internal_article_links.build_article_card_html）はリンク先記事のアイキャッチURLを
            他記事の本文へ静的HTMLとして埋め込むため、削除するとそのカードの画像が切れる。
            WordPressが生成するサイズ違い（-300x169.jpg 等）の参照も対象に含める。
        戻り値は結果を表す短い文字列（ログ用）。
        """
        if not old_media_id or old_media_id == new_media_id:
            return "skipped_no_old_media"
        r = self._request(
            "GET", f"{self.site_url}/wp-json/wp/v2/media/{old_media_id}",
            params={"context": "edit"}, raise_for_status=False,
        )
        if r.status_code == 404:
            return "skipped_already_deleted"
        r.raise_for_status()
        source_url = r.json().get("source_url") or ""
        path_part = source_url.split("/wp-content/uploads/")[-1]
        stem, _, ext = path_part.rpartition(".")
        filename_stem = stem.rsplit("/", 1)[-1]
        if not re.fullmatch(rf"{re.escape(note_key)}-thumb(?:-\d+)?(?:-scaled)?", filename_stem):
            return "skipped_not_sync_thumbnail"

        ref_pattern = re.compile(rf"{re.escape(stem)}(?:-\d+x\d+)?\.{re.escape(ext)}")
        for post_type in ("posts", "pages"):
            page_no = 1
            while True:
                r = self._request(
                    "GET", f"{self.site_url}/wp-json/wp/v2/{post_type}",
                    params={"search": filename_stem, "status": "any", "context": "edit",
                            "per_page": 100, "page": page_no, "orderby": "id", "order": "asc"},
                    raise_for_status=False,
                )
                if r.status_code == 400:  # 最終ページを超えた
                    break
                r.raise_for_status()
                items = r.json()
                for item in items:
                    if ref_pattern.search((item.get("content") or {}).get("raw", "") or ""):
                        return f"skipped_referenced_by_{post_type}_{item['id']}"
                if page_no >= int(r.headers.get("X-WP-TotalPages", 1) or 1):
                    break
                page_no += 1

        self._request(
            "DELETE", f"{self.site_url}/wp-json/wp/v2/media/{old_media_id}",
            params={"force": "true"},
        )
        return "deleted"

    def _post_media(self, image_bytes: bytes, content_type: str, filename: str) -> int | None:
        try:
            r = self._request(
                "POST", f"{self.site_url}/wp-json/wp/v2/media",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Type": content_type,
                },
                data=image_bytes,
                timeout=WP_REQUEST_TIMEOUT_HEAVY,
                raise_for_status=False,
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


NOTE_SINGLE_API_URL = "https://note.com/api/v3/notes/{key}"


def fetch_note_article_by_key(key: str) -> dict | None:
    """記事キーを指定してnote.com公開APIから1件分のメタデータを直接取得する（ログイン不要）。

    2026-10-18追記：note側で「クリエイターページに表示しない」設定
    （exclude_from_creator_top）にした公開記事は、fetch_note_article_list が使う
    一覧API（creators/{user}/contents）に一切出てこないため、--list-file で
    URLを指定しても同期できなかった（実機で2件の同期漏れを確認）。
    単体記事API（v3/notes/{key}）なら非表示設定の記事も取得できるので、
    一覧で見つからない記事のフォールバックとして使う。返す辞書は
    fetch_note_article_list の各要素と同じ形式。
    公開済みでない記事・別アカウントの記事・存在しないキーは None を返す。
    """
    resp = requests.get(
        NOTE_SINGLE_API_URL.format(key=key),
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    c = resp.json().get("data") or {}
    if (
        c.get("key") != key
        or c.get("status") != "published"
        or (c.get("user") or {}).get("urlname") != NOTE_USERNAME
    ):
        return None
    price = c.get("price") or 0
    return {
        "key": key,
        "title": c.get("name") or "",
        "price": price,
        "publish_at": c.get("publish_at") or "",
        "thumbnail": c.get("eyecatch") or "",
        "tags": list(dict.fromkeys(
            normalize_note_tag_name(h["hashtag"]["name"])
            for h in (c.get("hashtag_notes") or []) if h.get("hashtag")
            and normalize_note_tag_name(h["hashtag"]["name"])
        )),
        "paywall_element_id": c.get("separator"),
        "is_free": price == 0,
        "like_count": c.get("like_count", 0),
        "url": f"https://note.com/{NOTE_USERNAME}/n/{key}",
    }


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


VIDEO_TIMESTAMP_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "youtu.be", "www.youtu.be",
    "twitcasting.tv", "www.twitcasting.tv",
}


def protect_video_timestamp_links(html: str) -> str:
    """
    【2026-09-20修正：時間指定テキストリンクは削除ではなく保護する】
    note元記事の構造は、①YouTube/ツイキャス等の時間指定（?t=）付き
    テキストリンクと、②その直後の実際の埋め込み動画・カードの2つで
    構成されている。以前（2026-09-20時点のc17bea9）はこれを「同じURLへの
    冗長な重複」とみなして①のテキストリンクごと削除する実装
    （dedupe_redundant_bare_link_before_embed）をしていたが、これは仕様の
    誤認だった。①は「指定秒数へ飛ぶための本来必要なテキストリンク」で
    あり、消してはならない。

    本来の課題は「①のテキストリンクがWordPress側で勝手にoEmbed展開され、
    ②の埋め込みと合わせて動画が2連打されること」であって、リンクの
    存在そのものではない。そのため、時間指定パラメータ（?t=）を含む
    YouTube/ツイキャスへの<a>タグは、oEmbedの自動展開を確実に防ぐ
    明示的なHTMLリンク（target="_blank" rel="noopener noreferrer"）として
    保護しつつ、本文にそのまま残す。リンクを削除する処理は一切行わない。
    """
    if "<a " not in html and "<a>" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "t=" not in href:
            continue
        try:
            host = urlsplit(href).netloc.lower()
        except ValueError:
            continue
        if host not in VIDEO_TIMESTAMP_HOSTS:
            continue
        if a.get("target") != "_blank":
            a["target"] = "_blank"
            changed = True
        rel_tokens = set((a.get("rel") or []) if isinstance(a.get("rel"), list) else (a.get("rel") or "").split())
        needed = {"noopener", "noreferrer"}
        if not needed.issubset(rel_tokens):
            a["rel"] = " ".join(sorted(rel_tokens | needed))
            changed = True
    return str(soup) if changed else html


def ensure_visible_timestamp_link_before_embed(html: str) -> str:
    """
    【2026-09-20修正】note元記事の「①時間指定テキストリンク→②埋め込み動画」
    という構成を常に再現する。note.com側の編集画面によっては、時間指定
    （?t=）付きのYouTube/ツイキャス埋め込みが<figure data-src="URL?t=N">
    としてのみ存在し、URL自体は本文中の見える位置には一切表示されない
    （data-src属性の中にしか残らない）ケースがある。この場合、埋め込み
    プレーヤー自体は正しく指定秒数から再生されるが、読者が指定秒数の
    URLをコピー・共有する手段が本文上から失われてしまう。埋め込み直前に
    まだ同じURLへのテキストリンクが無ければ、クリック可能な<a>タグ
    （target="_blank" rel="noopener noreferrer"）として明示的に生成する。
    """
    if "<figure" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for figure in soup.find_all("figure"):
        target_url = (figure.get("data-src") or "").strip()
        if not target_url or "t=" not in target_url:
            continue
        try:
            host = urlsplit(target_url).netloc.lower()
        except ValueError:
            continue
        if host not in VIDEO_TIMESTAMP_HOSTS:
            continue

        prev = figure.find_previous_sibling()
        while prev is not None and prev.name == "p" and not prev.get_text(strip=True) and prev.find("a") is None:
            prev = prev.find_previous_sibling()
        if prev is not None and prev.name == "p":
            existing_links = prev.find_all("a", href=True)
            if len(existing_links) == 1 and existing_links[0]["href"].strip() == target_url:
                continue  # 既にテキストリンクが存在する

        p = soup.new_tag("p")
        a = soup.new_tag("a", href=target_url, target="_blank", rel="noopener noreferrer")
        a.string = target_url
        p.append(a)
        figure.insert_before(p)
        changed = True
    return str(soup) if changed else html


def fetch_note_body_html(page, url: str) -> str:
    page.goto(url, wait_until="load", timeout=30000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    # 【2026-10-11追記】networkidle待ち後もまれに本文コンテナ未描画のまま判定に進み
    # 「本文コンテナが見つかりませんでした」で失敗することを実機で確認したため、
    # いずれかの本文セレクタが現れるまで明示的に待つ。
    try:
        page.wait_for_selector(", ".join(NOTE_BODY_SELECTORS), timeout=15000)
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
    html = protect_video_timestamp_links(html)
    html = ensure_visible_timestamp_link_before_embed(html)
    html = normalize_twitter_embeds(html)
    html = rebuild_note_toc(html)
    html = strip_note_ui_buttons(html)
    return convert_external_article_embeds_to_blogcards(html)


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
    # 【2026-10-11修正】note側の目次DOMが <div role="presentation"><nav aria-label="目次"
    # class="mt-9"><details>... に変わりo-tableOfContentsクラスが消えたため、従来の
    # 文字列判定だけでは目次変換が丸ごとスキップされ、note生の「すべて表示」ボタン付き
    # HTMLがそのままWordPressへ書き込まれる事故が起きた（直近90日同期で61件）。
    # 判定はlink_converter.is_note_toc_nav()（クラス名 or aria-label="目次"）に一元化する。
    if "o-tableOfContents" not in html and 'aria-label="目次"' not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(is_note_toc_nav) is None:
        return html
    rebuild_toc(soup)
    return str(soup)


def strip_note_ui_buttons(html: str) -> str:
    """
    【2026-10-11追加】note.comのフロントエンド刷新後、コードブロック（<pre>）の横に
    「コピー」ボタン（<button data-id="ButtonIcon">＋SVGアイコン）が描画されるようになり、
    取得した本文にそのまま混入していた。note本文に著者がボタンを置くことはできないため、
    本文中の<button>はすべてnoteのUI部品とみなして除去し、ボタンを包んでいただけの
    空のdivも取り除く（WordPress上ではJSが無く押しても何も起きない無意味なボタンになる）。
    """
    if "<button" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for button in soup.find_all("button"):
        wrapper = button.parent
        button.decompose()
        while (
            wrapper is not None and wrapper.name == "div"
            and not wrapper.get_text(strip=True) and wrapper.find(["img", "iframe", "video", "pre"]) is None
        ):
            parent = wrapper.parent
            wrapper.decompose()
            wrapper = parent
    return str(soup)


TWITTER_WIDGETS_SCRIPT_SRC = "https://platform.x.com/widgets.js"


def normalize_twitter_embeds(html: str) -> str:
    """
    【2026-10-11追加】note本文中のX（Twitter）埋め込み（<figure embedded-service="twitter">）を、
    X公式の標準埋め込みコード（<blockquote class="twitter-tweet">＋widgets.js）へ正規化する。

    従来はnoteページ上でwidgets.jsが描画し終えた後のDOM（<div class="twitter-tweet-rendered">
    ＋高さ固定のiframe＋読み込むたびに変わるsessionId）をそのまま取り込んでいたため、
    たまたま描画が済んでいれば表示できていた。しかし2026-10頃のnote.comフロントエンド刷新で、
    取得時点のDOMが描画前の <blockquote data-class="twitter-tweet">（classではなく
    data-class属性）になり、widgets.jsがこれを認識できず、WordPress上でただの引用枠として
    表示される事故が起きた。

    note側がどちらの状態で返しても、常に「class="twitter-tweet"のblockquote＋widgets.js」に
    組み直す。widgets.jsが閲覧時に画像・動画付きのリッチカードを高さ自動で描画するうえ、
    出力HTMLが取得タイミングに左右されないため、差分検知のハッシュも安定する。
    """
    if 'embedded-service="twitter"' not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for figure in soup.find_all("figure", attrs={"embedded-service": "twitter"}):
        tweet_url = (figure.get("data-src") or "").strip()
        src_quote = figure.find("blockquote")

        quote = soup.new_tag("blockquote", attrs={"class": "twitter-tweet", "data-dnt": "true", "data-lang": "ja"})
        if src_quote is not None and src_quote.get_text(strip=True):
            # 描画前の状態：本文・投稿者・日付リンクを含む公式マークアップの中身をそのまま活かす
            for child in list(src_quote.contents):
                quote.append(child.extract())
        if not quote.find("a", href=re.compile(r"/status/\d+")):
            # 描画後（iframeのみ）の状態や中身が空の場合：widgets.jsが投稿を特定できるよう
            # ステータスURLへのリンクを必ず入れる
            if not tweet_url:
                iframe = figure.find("iframe", attrs={"data-tweet-id": True})
                if iframe is None:
                    continue  # 投稿URLを特定できない場合は触らない
                tweet_url = f"https://x.com/i/status/{iframe['data-tweet-id']}"
            a = soup.new_tag("a", href=tweet_url)
            a.string = tweet_url
            quote.append(a)

        script = soup.new_tag("script", attrs={"async": "", "src": TWITTER_WIDGETS_SCRIPT_SRC, "charset": "utf-8"})
        figure.clear()
        figure.append(quote)
        figure.append(script)
        changed = True
    return str(soup) if changed else html


EXTERNAL_BLOGCARD_AMP_SENTINEL = "AMP"  # 私用領域の文字。本文中の通常テキストと衝突しない
EXTERNAL_BLOGCARD_TITLE_MAX_LENGTH = 200
EXTERNAL_BLOGCARD_SNIPPET_MAX_LENGTH = 300


NOTE_WIDGET_BG_URL_RE = re.compile(r"""url\(\s*["']?([^"')]+)["']?\s*\)""")


def convert_external_article_embeds_to_blogcards(html: str) -> str:
    """noteの「外部サイトへのリンク埋め込み」（note編集画面でURLをそのまま貼ると
    生成される <figure embedded-service="external-article">...</figure> ウィジェット）
    を、サムネイル・タイトル・説明文・ドメイン付きの静的なブログカード
    （internal_article_links.build_blogcard_html。カード全体がhref付きの<a>1本）に
    置き換える。

    note.com側はこのウィジェットを自前のCSSでカード描画しているが、そのCSSは
    note.com上にしか無いため、そのまま持ち込むと青文字のリンクが並ぶだけになる
    （2026-09-13確認）。

    【2026-10-11 抜本改修：Cocoonの[blogcard]ショートコード出力を廃止】
    従来は [blogcard url="URL?title=...&snippet=..."] を出力し、描画をCocoonに委ねて
    いた。しかしCocoonの外部ブログカードは表示のたびに相手先へOGPをライブ取得する
    方式のため、(1) 外部リンクの多い記事では後半のカードが空になりクリックできない
    （CLAUDE.md 18項「既知の限界」）、(2) 相手先がボット拒否（403等）するとサムネイルが
    取れない、(3) URLに?title=...を埋め込むためリンク先URL自体が書き換わる、という
    構造的な問題を抱えていた。note側のウィジェットはタイトル・説明文・ドメインに加え、
    OGP画像をnote自身のCDN（assets.st-note.com）にキャッシュした
    <a class="external-article-widget-image" style="background-image:url(...)">も
    持っているため、これらを同期時にすべて確定させて静的HTML化する。リンク先が
    自分のnote/エキサイトブログ記事の場合は、後段の
    internal_article_links.convert_internal_links が内部カードへ組み直す。

    note.com自身の過去記事への埋め込み（embedded-service="note"）は対象外
    （internal_article_links.py が別途、自サイト内部リンク専用のカードに変換する）。
    """
    if 'embedded-service="external-article"' not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    changed = False
    for figure in soup.find_all("figure", attrs={"embedded-service": "external-article"}):
        url = (figure.get("data-src") or "").strip()
        if not url:
            a = figure.find("a", href=True)
            url = a["href"].strip() if a else ""
        if not url:
            continue  # URLが取れない場合は安全側に倒し、元のウィジェットをそのまま残す

        title_el = figure.find(class_="external-article-widget-title")
        desc_el = figure.find(class_="external-article-widget-description")
        domain_el = figure.find(class_="external-article-widget-url")
        image_el = figure.find(class_="external-article-widget-image")
        title = (title_el.get_text(strip=True) if title_el else "")[:EXTERNAL_BLOGCARD_TITLE_MAX_LENGTH]
        snippet = (desc_el.get_text(strip=True) if desc_el else "")[:EXTERNAL_BLOGCARD_SNIPPET_MAX_LENGTH]
        domain = domain_el.get_text(strip=True) if domain_el else ""
        thumbnail = ""
        if image_el is not None:
            m = NOTE_WIDGET_BG_URL_RE.search(image_el.get("style") or "")
            if m:
                thumbnail = m.group(1).strip()
            elif image_el.find("img") is not None:
                thumbnail = image_el.find("img").get("src") or ""

        is_internal = urlsplit(url).netloc.lower() == SITE_DOMAIN
        card_html = build_blogcard_html(
            url, title or url, snippet=snippet, thumbnail=thumbnail, domain=domain,
            internal=is_internal, element_id=figure.get("id"),
        )
        figure.replace_with(BeautifulSoup(card_html, "html.parser").find("a"))
        changed = True
    return str(soup) if changed else html



def finalize_external_blogcard_urls(content: str) -> str:
    """【2026-10-11以降】convert_external_article_embeds_to_blogcards は静的カードを出力し
    センチネルを生成しなくなったが、過去にショートコード方式で保存済みの本文を扱う
    バックフィル系スクリプトとの互換のため残している。以下は旧方式の説明。

    convert_external_article_embeds_to_blogcards が退避させたセンチネルを
    実際の"&"へ戻す。本文の加工が完全に終わり、WordPressへ送信する直前
    （wp.create_post / wp.update_post の直前）で必ず最後に1回だけ呼ぶこと。
    途中に convert_internal_links 等、追加でBeautifulSoupのシリアライズを
    挟む処理を新設した場合、その後段で必ずこれを呼び直す必要がある。
    """
    return content.replace(EXTERNAL_BLOGCARD_AMP_SENTINEL, "&")


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
                    content = finalize_external_blogcard_urls(content)
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

                    thumbnail_url = ""
                    if a["thumbnail"]:
                        media_id = wp.upload_media_from_url(a["thumbnail"], f"{a['key']}-thumb.jpg")
                        if media_id:
                            wp.update_post(post_id, {"featured_media": media_id})
                            thumbnail_url = a["thumbnail"]

                    new_hash = body_hash(body_html)
                    state.setdefault("note", {})[a["key"]] = {"body_hash": new_hash, "price": a["price"]}
                    if thumbnail_url:
                        # 更新追従（sync_note_updates）でアイキャッチ差し替えの要否を判定する基準値
                        state["note"][a["key"]]["thumbnail_url"] = thumbnail_url

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
    window_days: int | None = None,
) -> list[dict]:
    # 【2026-09-13追記】月次メンテナンス（sync_monthly_maintenance.py）から
    # 「直近4ヶ月」等、週次の既定値（NOTE_UPDATE_WINDOW_DAYS=30日）より広い
    # ウィンドウで呼び出せるようにパラメータ化した。省略時は従来通り既定値を使う。
    window_days = NOTE_UPDATE_WINDOW_DAYS if window_days is None else window_days
    force_keys = force_keys or set()
    # 【2026-10-18追記】sync_updated_notes.py --list-file のように渡された記事が
    # すべて強制指定の場合は、公開日ウィンドウが一切効かないため見出しを切り替える
    # （--key 指定時は通常の更新追従と混在するので従来の見出しのまま）。
    all_forced = bool(force_keys) and all(a["key"] in force_keys for a in articles)

    print("\n" + "=" * 60)
    if all_forced:
        print("【3】note記事の強制上書き更新（リスト指定・公開日/差分判定なし）")
    else:
        print(f"【3】note記事の更新追従（公開から{window_days}日以内）")
    print("=" * 60)

    now = datetime.now()
    recent = []
    for a in articles:
        if a["key"] in force_keys:
            # 【2026-09-09追記：即時更新の強制指定】--key で明示された記事は、
            # ウィンドウの対象外（古い記事の後追い修正等）であっても必ず含める。
            recent.append(a)
            continue
        if not a["publish_at"]:
            continue
        try:
            pub_dt = datetime.strptime(a["publish_at"][:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        if (now - pub_dt).days <= window_days:
            recent.append(a)
    if all_forced:
        print(f"強制上書き対象のnote記事: {len(recent)}件")
    else:
        print(f"公開から{window_days}日以内のnote記事: {len(recent)}件"
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
                    # 【2026-10-18追記：アイキャッチ画像の差し替え漏れの修正】
                    # 従来この更新経路はfeatured_mediaに一切触れておらず、note側でヘッダー
                    # 画像だけを差し替えても（例: n488640bb2088）WordPress側は新規投稿時の
                    # 古い画像のまま残っていた。前回反映したnote側画像URLをstateに記録し、
                    # URLが変わっていれば差し替える。記録の無い過去記事は、通常の差分同期で
                    # 全件を一斉に再アップロードしないよう、初回は現在のURLを基準値として
                    # 記録するだけにする（強制指定の記事は記録の有無にかかわらず必ず取り直す）。
                    recorded_thumb = note_state.get(key, {}).get("thumbnail_url")
                    current_thumb = a.get("thumbnail") or ""
                    thumb_changed = bool(current_thumb) and recorded_thumb is not None and recorded_thumb != current_thumb

                    if old_hash == new_hash and not forced and not thumb_changed:
                        if execute and recorded_thumb is None and current_thumb and key in note_state:
                            note_state[key]["thumbnail_url"] = current_thumb
                        print("    変更なし")
                        time.sleep(REQUEST_DELAY_SECONDS)
                        continue

                    if forced and old_hash == new_hash:
                        print("    [強制更新]")
                    elif old_hash == new_hash:
                        print("    [アイキャッチ画像の変更を検出]")
                    else:
                        print("    [変更を検出]")
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
                    content = finalize_external_blogcard_urls(content)
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

                    # アイキャッチの差し替えも同じ更新リクエストに載せる（この後のCodoc再紐付けより
                    # 前に済ませておかないと、featured_mediaの更新で紐付けが外れる恐れがあるため）。
                    # アップロードに失敗した場合は既存の画像を残し、次回の強制同期で取り直せるよう
                    # 基準値は更新しない。
                    thumbnail_url = recorded_thumb
                    if current_thumb and (forced or thumb_changed or recorded_thumb is None):
                        media_id = wp.upload_media_from_url(current_thumb, f"{key}-thumb.jpg")
                        if media_id:
                            update_payload["featured_media"] = media_id
                            thumbnail_url = current_thumb
                            print(f"    [アイキャッチ] 最新のnote画像に差し替えます（media_id={media_id}）")
                        else:
                            print("    [警告] アイキャッチ画像のアップロードに失敗したため、既存の画像を維持します")

                    wp.update_post(post["id"], update_payload)
                    note_state[key] = {"content_hash": new_hash, "price": existing_price}
                    if thumbnail_url:
                        note_state[key]["thumbnail_url"] = thumbnail_url
                    print(f"    [OK] post_id={post['id']} を更新しました")
                    log_rows.append(log_row("update_note", post["id"], slug, a["title"], "success"))

                    # 新しいアイキャッチの紐付けが成功した後に限り、差し替え前の旧画像を削除する
                    # （安全条件は WP.delete_replaced_featured_media を参照）。失敗しても同期は止めない。
                    if "featured_media" in update_payload:
                        old_media_id = post.get("featured_media") or 0
                        try:
                            del_result = wp.delete_replaced_featured_media(
                                old_media_id, update_payload["featured_media"], key,
                            )
                        except Exception as e:
                            del_result = f"error: {e}"
                        if del_result == "deleted":
                            print(f"    [アイキャッチ] 差し替え前の旧画像を削除しました（media_id={old_media_id}）")
                        elif old_media_id:
                            print(f"    [アイキャッチ] 旧画像（media_id={old_media_id}）は削除しませんでした: {del_result}")
                        if old_media_id:
                            log_rows.append(log_row(
                                "update_note", post["id"], slug, a["title"],
                                "old_thumbnail_deleted" if del_result == "deleted" else "old_thumbnail_kept",
                                f"media_id={old_media_id} {del_result}",
                            ))

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

def fetch_all_wp_posts_with_dates(wp: WP, categories: int | None = None) -> list[dict]:
    """全件（または指定カテゴリーのみ）の投稿をid/slug/date/titleだけ軽量取得する。

    2026-09-13追記：sync_codoc_discount専用に categories 引数を追加した。
    Codocの有料設定（[[codoc/codoc-block]]）が存在しうるのはnoteカテゴリー
    （NOTE_CATEGORY_ID）記事約358件のみで、エキサイトブログ等の残り約2400件を
    毎回全件走査するのは無駄な負荷・実行時間（数十分）を生んでいた
    （実機で確認：値下げ対象の絞り込みそのものはこの関数の後段で
    codocブロックの有無を個別確認しているため、走査母数を減らしても
    正しさは変わらず、単に無駄な母数を削るだけ）。

    また、5項（CLAUDE.md）で確立した教訓に従い、明示的に
    orderby=id&order=asc を指定して安定したページネーションを保証する
    （既定のdate DESC順は、同一タイムスタンプの一括移行記事が多いこの
    サイトではページ間の重複・欠落を引き起こす既知の不具合パターン）。
    """
    posts = []
    page = 1
    params = {
        "per_page": 100,
        "status": "publish,future,draft,pending,private",
        "context": "edit", "_fields": "id,slug,date,title",
        "orderby": "id", "order": "asc",
    }
    if categories:
        params["categories"] = categories
    while True:
        # 2026-09-13追記：全件（数千件規模）を数十ページに分けて取得するため、
        # 1ページのタイムアウトでも巻き込まれてクラッシュしないよう
        # wp._request（リトライ＋タイムアウト延長つき）経由に統一した。
        r = wp._request(
            "GET", f"{wp.site_url}/wp-json/wp/v2/posts",
            params={**params, "page": page},
            raise_for_status=False,
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
        time.sleep(0.3)
    return posts


def fetch_posts_content_by_ids(wp: WP, ids: list[int]) -> dict[int, str]:
    """指定したID群の投稿本文（content.raw）を、100件ずつの一括取得（WP REST APIの
    ?include= パラメータ）でまとめて取得する。

    2026-09-13追記：sync_codoc_discountが対象記事1件ごとにwp.get_post()を
    個別に呼んでいたため、noteカテゴリー358件へ走査対象を絞った後も
    294件の個別リクエスト＋1秒間隔のウェイトで6分半かかっていた
    （実機で確認）。本文はそもそも一括取得できるフィールドのため、
    100件ずつ最大でも4リクエストにまとめることで、同じ情報量を
    大幅に少ないリクエスト数で取得しサーバー負荷・実行時間の両方を削減する。
    """
    content_by_id: dict[int, str] = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        r = wp._request(
            "GET", f"{wp.site_url}/wp-json/wp/v2/posts",
            params={
                "include": ",".join(str(x) for x in chunk),
                "per_page": 100, "context": "edit",
                "_fields": "id,content",
            },
        )
        for item in r.json():
            content_by_id[item["id"]] = item.get("content", {}).get("raw", "") or ""
        if i + 100 < len(ids):
            time.sleep(0.3)
    return content_by_id


def sync_codoc_discount(wp: WP, execute: bool, limit: int | None) -> list[dict]:
    print("\n" + "=" * 60)
    print(f"【4】Codoc自動値下げ（投稿から{CODOC_DISCOUNT_AFTER_DAYS}日以上経過 かつ 価格>{CODOC_DISCOUNT_PRICE}円）")
    print("=" * 60)

    # 2026-09-13追記：Codocの有料設定（wp:codoc/codoc-block）が存在しうるのは
    # noteカテゴリー記事（約358件）のみで、エキサイトブログ等（約2400件）は
    # そもそも対象外であることが確認済みのため、走査母数をnoteカテゴリーのみに
    # 限定し、全2810件規模の無駄な走査（数十分）を避ける。
    posts = fetch_all_wp_posts_with_dates(wp, categories=NOTE_CATEGORY_ID)
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
    print(f"noteカテゴリー記事: {len(posts)}件 / 投稿から{CODOC_DISCOUNT_AFTER_DAYS}日以上経過: {len(old_posts)}件（Codocブロックの有無はこれから個別確認）")

    if limit:
        old_posts = old_posts[:limit]

    # 2026-09-13追記：本文はここで一括取得する（fetch_posts_content_by_ids参照）。
    # 以降のループは基本的にネットワーク往復なしでメモリ上のcontentを見るだけになるため、
    # 実際にwp.update_post()で書き込みが発生する記事以外ではREQUEST_DELAY_SECONDSの
    # ウェイトも不要（＝大半の記事は待たずに次へ進む）。
    content_by_id = fetch_posts_content_by_ids(wp, [p["id"] for p in old_posts])

    log_rows = []
    for i, p in enumerate(old_posts, start=1):
        title = p["title"]["raw"] if isinstance(p.get("title"), dict) else str(p.get("title", ""))
        wrote = False
        # 2026-09-13追記：本文解析やwp.update_post（ReadTimeout等）で1件失敗しても
        # スクリプト全体を停止させず、失敗をログに記録して次の記事へ進める。
        # wp._request 側で最大3回まで自動リトライ済みのため、ここに到達する
        # RequestExceptionは「リトライしても解消しなかった」ケースのみ。
        try:
            content = content_by_id.get(p["id"], "")
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
            wrote = True
            print("    [OK] 値下げしました")
            log_rows.append(log_row("discount_codoc", p["id"], p["slug"], title, "success", f"{price}->{CODOC_DISCOUNT_PRICE}"))
        except requests.RequestException as e:
            print(f"    [失敗] id={p['id']}: 通信エラー（{type(e).__name__}: {e}）。次の記事へ続行します")
            log_rows.append(log_row("discount_codoc", p["id"], p.get("slug", ""), title, "failed", str(e)))
        except Exception as e:
            print(f"    [失敗] id={p['id']}: {e}")
            log_rows.append(log_row("discount_codoc", p["id"], p.get("slug", ""), title, "failed", str(e)))
        finally:
            # 実際にwp.update_post()を呼んだ記事の直後だけウェイトを入れる
            # （本文はすでに一括取得済みのため、対象外の記事ではネットワーク
            # 往復自体が発生せず、待つ理由がない）。
            if wrote:
                time.sleep(REQUEST_DELAY_SECONDS)

    return log_rows


def relink_touched_posts(wp: WP, log_rows: list[dict], execute: bool) -> None:
    """【2026-09-02追記→2026-09-13関数化】content更新に限らずcategories等の更新
    だけでもCodoc側の購読プラン紐付けが解除される副作用が確認されている
    （backfill_internal_links.py・categorize_articles_ai.py のdocstring参照）。
    個別関数ごとに再紐付け処理を書き込む方式は書き漏れのリスクが常に残るため、
    今回実際にWordPress側が更新された投稿（success系の結果を持つ行）を
    横断的に集約し、Codoc有料記事であれば最後に一括で再紐付けを確認する。
    sync_new_note_posts / sync_note_updates は既に個別に再紐付け済みだが、
    二重実行しても process_entry は冪等（既にlinked済みならスキップ）なので
    無害。sync_codoc_discount 等、個別対応していない経路の取りこぼしを
    ここで一括して拾う。

    元は main() 内に直書きされていたが、sync_weekly.py / sync_monthly_maintenance.py
    からも同じ安全網を再利用したいため独立関数化した（動作は変更なし）。
    """
    if not execute:
        return
    touched_ids = sorted({
        row["post_id"] for row in log_rows
        if row.get("post_id") and row.get("result") == "success"
    })
    if not touched_ids:
        return
    print("\n" + "=" * 60)
    print(f"Codoc購読プラン紐付けの横断チェック（今回更新した{len(touched_ids)}件）")
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


# ==================== メイン ====================

def regenerate_tag_index_page(wp: WP, creds: dict) -> None:
    """
    サイドバー「キーワードから探す（50音順）」用のタグ一覧固定ページ（/tags/）を
    最新のタグ使用状況に合わせて再生成する。note記事の新規投稿・更新追従の
    どちらでもタグが増減しうるため、それらの経路の最後に必ず呼ぶこと。
    【2026-09-20追記】auto_sync_blogs.py本体（週次/月次/差分同期すべてが
    経由するコアモジュール）とsync_weekly.py・sync_updated_notes.pyの
    双方から共通利用できるよう、main()内にベタ書きされていた処理を
    関数として切り出した。
    """
    print("\n" + "=" * 60)
    print("サイドバー「キーワードから探す」タグ一覧ページの自動再生成")
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

    print("\n" + "=" * 60)
    print("【5】Codoc購読プラン紐付けの横断チェック")
    relink_touched_posts(wp, all_log_rows, args.execute)

    # 【2026-09-09追記：サイドバー「キーワードから探す（50音順）」の自動最新化】
    # note記事の新規投稿・更新追従（タグ同期を含む）でWordPressのタグ
    # （post_tag）が増減しうるのは new-note と update-note の2経路のみ。
    # 従来はタグ一覧固定ページ（/tags/、generate_tag_index.py）の再生成が
    # 手動運用のままだったため、新しいキーワードのタグを記事に付けても
    # サイドバーの一覧に反映されるまで誰かが手動でスクリプトを実行するまで
    # 気づかれない状態だった。ここで毎回の本番実行の最後に自動で再生成・
    # 固定ページへ反映することで、以後は同期のたびに必ず追従する。
    if args.execute and (args.only in (None, "new-note", "update-note") or args.key):
        regenerate_tag_index_page(wp, creds)

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
