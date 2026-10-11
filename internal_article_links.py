"""
記事本文中の note.com / rakusen.exblog.jp（自分の過去記事）への外部リンクを、
サイト内（junchan-world.com）の対応記事への内部リンクに変換する共通モジュール。

auto_sync_blogs.py（新規投稿・更新時）と backfill_internal_links.py（過去記事一括変換）
の両方から利用される。

変換対象は2種類:
    1. 通常のテキストリンク（<a href="https://note.com/tweettv/n/{key}">等）。
       こちらは文中リンクなのでプレーンテキストリンクのまま内部URLに置き換える。
    2. note編集画面で自分の記事URLをそのまま貼った際に生成される「ブログカード」
       （<figure embedded-service="note"> 配下に <iframe src="https://note.com/
       embed/notes/{key}"> を含む埋め込みウィジェット）。このiframeはnote.com側の
       ページをそのまま読み込んで表示するため、hrefだけを書き換えても見た目上の
       カード（iframe内部）をクリックするとnote.comへ遷移してしまう（実機で確認
       済み）。そのため対応記事が存在する場合は、カード自体を「アイキャッチ画像＋
       タイトル＋junchan-world.com表記」を持つ自サイト用のリッチなカード型HTML
       （インラインstyleで完結する<a>ブロック。テーマCSSに依存しない）に置き換える。
       過去に（旧ロジックで）単純な文字リンク<p class="note-embed-link">まで
       ダウングレードされてしまった記事も、このリッチカードへ再アップグレードする
       （_upgrade_simple_embed_links）。

【絶対厳守】サブスク案内の赤枠（SUBSCRIPTION_UPSELL_BOX_TEMPLATE、
border: 2px solid #ff7b7b）内にある「元記事（note.com）への確認・購入用リンク」は
変換対象から除外する（元のnote.comリンクのまま維持する）。

対応記事の特定方法:
    WordPress側の「note」カテゴリー（ID 2471）・「エキサイトブログ」カテゴリー
    （ID 2445）配下の投稿は、auto_sync_blogs.py により
        note記事:   スラッグ = note.com側のkey（例: n8a33d850cd3c）
        exblog記事: スラッグ = "exblog-{記事ID}"
    という規則で必ず作成されている（generate_perfect_wp_xml.py等の一括移行を
    含め、note/exblogカテゴリーの投稿はこの2種類の自動投稿ルートしか存在しない。
    カテゴリー投稿数とスラッグ規則に合致する投稿数が完全一致することを実機で
    確認済み: note=324件, exblog=338件）ため、カスタムフィールド等を介さず
    スラッグから直接、対応する記事の有無を判定できる。
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

NOTE_CATEGORY_ID = 2471
EXBLOG_CATEGORY_ID_FOR_LINKS = 2445

NOTE_USERNAME = "tweettv"
NOTE_HOSTS = {"note.com", "www.note.com"}
EXBLOG_HOST = "rakusen.exblog.jp"

# 赤枠(サブスク案内ボックス)の枠線色。このインラインstyleを持つ要素配下は変換対象外とする。
RED_BOX_STYLE_MARKER = "#ff7b7b"

NOTE_KEY_RE = re.compile(r"^n[0-9a-f]{6,}$")
EXBLOG_ID_RE = re.compile(r"^/?(\d+)/?$")


def _extract_thumbnail_url(post: dict) -> str:
    """_embed=wp:featuredmedia で埋め込まれたアイキャッチ画像から、カード表示に
    適当なサイズのURLを取り出す。アイキャッチが無い投稿では空文字を返す。"""
    media_list = (post.get("_embedded") or {}).get("wp:featuredmedia") or []
    if not media_list:
        return ""
    media = media_list[0]
    if not isinstance(media, dict) or media.get("code"):
        return ""  # アイキャッチ未設定時、WPは埋め込みの代わりにエラーオブジェクトを返す
    sizes = ((media.get("media_details") or {}).get("sizes")) or {}
    for size_name in ("medium", "thumb320", "thumbnail"):
        if size_name in sizes and sizes[size_name].get("source_url"):
            return sizes[size_name]["source_url"]
    return media.get("source_url", "")


def classify_slug(slug: str) -> tuple[str, str] | None:
    """スラッグから ("note"|"exblog", key) を判定する。どちらでもなければNone。"""
    if NOTE_KEY_RE.match(slug):
        return ("note", slug)
    if slug.startswith("exblog-"):
        return ("exblog", slug[len("exblog-"):])
    return None


def fetch_link_maps(wp) -> tuple[dict[str, dict], dict[str, dict]]:
    """
    WordPress側の全投稿を走査し、note記事key / exblog記事ID ->
    {id, slug, link, title, thumbnail} の対応表を作る。

    【重要】カテゴリー（note=2471 / エキサイトブログ=2445）では絞り込まない。
    2026-08-11以前は auto_sync_blogs.py が新規note記事を categories=[1]
    （Uncategorized）で投稿するバグがあり、日々の自動同期で追加される記事が
    「note」カテゴリー（2471）に入らないまま公開されていた（サイドバーの
    絞り込み検索から漏れる不具合として実機で発覚・修正済み。CLAUDE.md
    「8. note記事のカテゴリー割り当て」参照）。現在は新規投稿時に
    NOTE_CATEGORY_ID を必ず設定するよう修正済みだが、この対応表はそれとは
    独立した用途（内部リンク変換の対象記事特定）のため、カテゴリー整合性の
    バグに引きずられて内部リンク化まで漏れることが二度と起きないよう、
    引き続きカテゴリーに依存せずauto_sync_blogs.py が必ず守っている
    スラッグ規則（note記事="n{key}"、exblog記事="exblog-{記事ID}"）で
    全投稿から判定する。
    """
    note_map: dict[str, dict] = {}
    exblog_map: dict[str, dict] = {}

    page = 1
    while True:
        r = wp.session.get(
            f"{wp.site_url}/wp-json/wp/v2/posts",
            params={
                "per_page": 100,
                "page": page,
                # id昇順で固定する。既定の並び順（date DESC）は、一括移行時に同一
                # 日時で作成された投稿が大量にあるとタイ（同着）の順序がページ間で
                # 安定せず、ページネーション中に投稿が重複・欠落することがある
                # （実機で確認済み：同じクエリを2回投げただけで対応表から特定の
                # 投稿が抜け落ちるケースがあった）。idは一意なのでこれを避けられる。
                "orderby": "id",
                "order": "asc",
                "status": "publish,future,draft,pending,private",
                "context": "edit",
                "_embed": "wp:featuredmedia",
                # _embedded は _links の解決結果として付与されるため、_fields に
                # _links も含めないと _embedded ごと消えてしまう（実機で確認済み）。
                "_fields": "id,slug,link,title,featured_media,_links,_embedded",
            },
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for p in batch:
            classified = classify_slug(p["slug"])
            if not classified:
                continue
            kind, key = classified
            target_map = note_map if kind == "note" else exblog_map
            title_field = p.get("title") or {}
            title = title_field.get("raw") or title_field.get("rendered") or ""
            target_map[key] = {
                "id": p["id"],
                "slug": p["slug"],
                "link": p["link"],
                "title": title,
                "thumbnail": _extract_thumbnail_url(p),
            }
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1

    return note_map, exblog_map


def list_target_posts(wp) -> list[dict]:
    """
    バックフィル対象となる全投稿（note記事・exblog記事のスラッグ規則に合致する
    投稿のみ）を {id, slug, link, title} のリストで返す。fetch_link_maps と同様、
    カテゴリーではなくスラッグ規則で判定する（理由は fetch_link_maps のdocstring
    を参照）。
    """
    posts: list[dict] = []
    page = 1
    while True:
        r = wp.session.get(
            f"{wp.site_url}/wp-json/wp/v2/posts",
            params={
                "per_page": 100,
                "page": page,
                # id昇順で固定する（理由は fetch_link_maps を参照）。
                "orderby": "id",
                "order": "asc",
                "status": "publish,future,draft,pending,private",
                "context": "edit",
                "_fields": "id,slug,link,title",
            },
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for p in batch:
            if classify_slug(p["slug"]):
                posts.append(p)
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return posts


def _match_target(href: str) -> tuple[str, str] | None:
    """hrefが自分のnote/exblog記事を指していれば ("note"|"exblog", key) を返す。それ以外はNone。"""
    try:
        parsed = urlparse(href)
    except ValueError:
        return None

    host = parsed.netloc.lower()

    if host in NOTE_HOSTS:
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[0] == NOTE_USERNAME and parts[1] == "n" and NOTE_KEY_RE.match(parts[2]):
            return ("note", parts[2])
        return None

    if host == EXBLOG_HOST:
        m = EXBLOG_ID_RE.match(parsed.path)
        if m:
            return ("exblog", m.group(1))
        return None

    return None


def _is_in_red_box(tag) -> bool:
    for ancestor in tag.parents:
        if hasattr(ancestor, "get") and RED_BOX_STYLE_MARKER in (ancestor.get("style") or ""):
            return True
    return False


NOTE_EMBED_IFRAME_RE = re.compile(r"note\.com/embed/notes/([0-9a-z]+)")
NOTE_EMBED_DATA_SRC_RE = re.compile(r"note\.com/" + re.escape(NOTE_USERNAME) + r"/n/([0-9a-z]+)")


def _extract_note_embed_key(figure) -> str | None:
    """
    note の「ブログカード」埋め込みウィジェット（<figure>...<iframe src=
    "https://note.com/embed/notes/{key}">...）から記事keyを取り出す。
    自分の記事以外（他ユーザーの記事等）を誤変換しないよう、iframeのsrc
    （note.com/embed/notes/{key}、これは常に自分のnoteのブログカードでのみ
    生成される）を主に見る。
    """
    iframe = figure.find("iframe", src=True) if hasattr(figure, "find") else None
    if iframe:
        m = NOTE_EMBED_IFRAME_RE.search(iframe.get("src") or "")
        if m:
            return m.group(1)

    data_identifier = figure.get("data-identifier") or ""
    if NOTE_KEY_RE.match(data_identifier):
        return data_identifier

    m = NOTE_EMBED_DATA_SRC_RE.search(figure.get("data-src") or "")
    if m:
        return m.group(1)

    return None


# カードのデザインはインラインstyleのみで完結させる（テーマ側CSSに依存すると、
# テーマ変更時に崩れたり、REST経由の投稿ではエディタのブロックCSSが効かないため）。
_CARD_LINK_STYLE = (
    "display:block;text-decoration:none;color:#222;border:1px solid #e0e0e0;"
    "border-radius:10px;overflow:hidden;margin:1em 0;background:#fff;"
    "max-width:494px;box-shadow:0 1px 3px rgba(0,0,0,0.08);"
)
_CARD_THUMB_STYLE = (
    "display:block;width:100%;height:200px;background-color:#f0f0f0;"
    "background-image:url('{thumb}');background-size:cover;background-position:center;"
)
_CARD_BODY_STYLE = "display:block;padding:0.8em 1em;"
_CARD_TITLE_STYLE = (
    "display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;-webkit-box-orient:vertical;"
    "overflow:hidden;font-weight:bold;font-size:0.95em;line-height:1.5;color:#222;"
)
_CARD_DOMAIN_STYLE = "display:block;margin-top:0.6em;font-size:0.75em;color:#999;"


def build_article_card_html(target: dict) -> str:
    """アイキャッチ画像＋タイトル＋サイトドメイン表記を持つ、自サイト用の
    リッチなブログカードHTML（インラインstyleのみ・<a>1本で完結）を組み立てる。"""
    title = html.escape(target.get("title") or target["link"])
    thumb_html = ""
    if target.get("thumbnail"):
        thumb_style = _CARD_THUMB_STYLE.format(thumb=target["thumbnail"])
        thumb_html = f'<span style="{thumb_style}"></span>'
    return (
        f'<a class="internal-article-card" href="{target["link"]}" style="{_CARD_LINK_STYLE}">'
        f"{thumb_html}"
        f'<span style="{_CARD_BODY_STYLE}">'
        f'<span style="{_CARD_TITLE_STYLE}">{title}</span>'
        f'<span style="{_CARD_DOMAIN_STYLE}">junchan-world.com</span>'
        f"</span>"
        f"</a>"
    )


def _card_tag_for(soup: BeautifulSoup, target: dict, source_id: str | None) -> "BeautifulSoup":
    card_soup = BeautifulSoup(build_article_card_html(target), "html.parser")
    card_tag = card_soup.find("a")
    if source_id:
        card_tag["id"] = source_id
    return card_tag


def _replace_note_embed_cards(soup: BeautifulSoup, note_map: dict[str, dict]) -> int:
    """
    自分のnote記事への「ブログカード」埋め込みのうち、サイト内に対応記事が
    存在するものだけを、自サイト用のリッチなカードHTMLに置き換える。
    iframeはnote.com側のページをそのまま表示するため、hrefの書き換えだけでは
    見た目のカードをクリックした際にnote.comへ遷移してしまう（カードの表示領域
    そのものがnote.com製のiframeであるため）。
    """
    count = 0
    for figure in soup.find_all("figure"):
        if _is_in_red_box(figure):
            continue
        key = _extract_note_embed_key(figure)
        if not key:
            continue
        target = note_map.get(key)
        if not target:
            continue  # サイト内に対応記事が無ければnote.comの埋め込みカードのまま維持

        figure.replace_with(_card_tag_for(soup, target, figure.get("id")))
        count += 1
    return count


def _upgrade_simple_embed_links(soup: BeautifulSoup, note_map: dict[str, dict]) -> int:
    """
    過去のバックフィル実行（旧ロジック）で、ブログカードが単純な文字リンク
    <p class="note-embed-link"><a href="https://junchan-world.com/{key}/">...</a></p>
    にまで変換されてしまった記事を、リッチなカードHTMLへ再アップグレードする。
    """
    count = 0
    for p in soup.find_all("p", class_="note-embed-link"):
        if _is_in_red_box(p):
            continue
        a = p.find("a", href=True)
        if not a:
            continue
        slug = urlparse(a["href"]).path.strip("/")
        target = note_map.get(slug)
        if not target:
            continue

        p.replace_with(_card_tag_for(soup, target, p.get("id")))
        count += 1
    return count


# Cocoonテーマの[blogcard]ショートコード（初回一括移行 generate_perfect_wp_xml.py が
# 生成した記事に見られる、note埋め込みカードの第三の形式）。ページ表示のたびに
# Cocoon側がリンク先のOG情報をライブ取得して描画するため、取得先が一時的なサーバー
# エラー（507 Insufficient Storage等）を返すと、そのエラー応答テキストがカードの
# タイトル/画像部分にそのまま露出してしまう不具合が実機で確認されている。
# ライブ取得に依存しない静的なリッチカード（build_article_card_html）へ置き換える
# ことで、この種のエラーが二度と表面化しないようにする。
#
# 出力例（囲む<p class="note-embed-link">はネストしたpタグを含む不正なHTMLで、
# 閉じタグの対応が壊れているため、BeautifulSoupでの解析前に正規表現で該当箇所
# だけを安全に置換する（BeautifulSoupに丸ごと食わせると、壊れたネストにより
# 後続の全段落を誤って同じ要素の中身とみなして巻き込んでしまうおそれがある）。
#   <p class="note-embed-link" id="...">
#   <p>[blogcard url="https://note.com/tweettv/n/{key}"]</p>
BLOGCARD_SHORTCODE_PATTERN = re.compile(
    r'<p class="note-embed-link" id="[0-9a-f-]+">\s*<p>\[blogcard url="([^"]+)"\]</p>'
)

# 同じ[blogcard]ショートコードが、見出し直後に挿入された場合は<h3>タグで囲まれる
# 別バリエーションもある（実機で確認済み）。こちらは開始・終了タグの対応が取れて
# いる（壊れたネストではない）ため、上のパターンほど危険ではないが、同様に
# ライブ取得依存を無くすため検知して置き換える。
BLOGCARD_SHORTCODE_H3_PATTERN = re.compile(
    r'<h3 id="[0-9a-f-]+" name="[0-9a-f-]+" tabindex="-1">\s*<p>\[blogcard url="([^"]+)"\]</p>\s*</h3>'
)


# 【2026-10-11追記】auto_sync_blogs.convert_external_article_embeds_to_blogcards が
# noteの外部リンク埋め込み（<figure embedded-service="external-article">）から生成する、
# 入れ子の壊れていない素の形式。URLにはタイトル・説明文の上書き用クエリ
# （?title=...&snippet=...）が付く。従来は上の2パターンしか扱っておらず、note記事内で
# 自分のエキサイトブログ記事（rakusen.exblog.jp/{ID}）を外部カードとして貼っていると、
# サイト内に対応記事（exblog-{ID}）があっても外部URLのまま残り、403で開けない
# エキサイトブログ側へ飛ばされる（＝クリックしてもページが開かない）不具合になっていた。
BLOGCARD_SHORTCODE_PLAIN_PATTERN = re.compile(
    r'<p>\s*\[blogcard url="([^"]+)"\]\s*</p>'
)


def _resolve_blogcard_target(url: str, note_map: dict[str, dict], exblog_map: dict[str, dict]) -> dict | None:
    match = _match_target(url)
    if match:
        kind, key = match
        return (note_map if kind == "note" else exblog_map).get(key)

    # 既に自サイトURL（https://junchan-world.com/{slug}/）になっている場合はスラッグで判定する。
    parsed = urlparse(url)
    if "junchan-world.com" not in parsed.netloc:
        return None
    slug = parsed.path.strip("/")
    return note_map.get(slug) or exblog_map.get(slug)


def replace_blogcard_shortcodes(
    content_html: str,
    note_map: dict[str, dict],
    exblog_map: dict[str, dict],
) -> tuple[str, int]:
    """[blogcard url="..."]ショートコード（壊れたネスト構造ごと）を、サイト内に
    対応記事があるものだけリッチカードHTMLへ置き換える。BeautifulSoup解析前の
    生文字列に対して行う（理由は上のコメントを参照）。

    対応記事が見つからない場合でも、壊れたネスト構造（<p class="note-embed-link">
    の中に閉じタグの対応しない<p>が入れ子になっている状態）だけは、外側の
    <p>を1つに正規化して解消する。これを怠ると、この関数の後段でこの文字列を
    BeautifulSoupで解析した際、閉じられていない<p>が後続の段落をすべて自分の
    子要素として飲み込んでしまい（実機で確認済み）、後段の処理（
    _upgrade_simple_embed_links等）がその「飲み込まれた」要素ごと誤って
    置換・削除してしまう重大な事故につながる。
    """
    if "[blogcard" not in content_html:
        return content_html, 0

    count = 0

    def _sub_nested_p(m: re.Match) -> str:
        nonlocal count
        url = m.group(1)
        target = _resolve_blogcard_target(url, note_map, exblog_map)
        if target:
            count += 1
            return build_article_card_html(target)
        # 対応記事が無い場合も、壊れたネストだけは安全な単一<p>に正規化しておく
        # （ショートコード自体はCocoonのライブ描画に任せるため維持する）。
        return f'<p class="note-embed-link">[blogcard url="{html.escape(url)}"]</p>'

    def _sub_h3(m: re.Match) -> str:
        nonlocal count
        url = m.group(1)
        target = _resolve_blogcard_target(url, note_map, exblog_map)
        if not target:
            return m.group(0)  # 開始・終了タグの対応は取れているため、無変換でも安全
        count += 1
        return build_article_card_html(target)

    def _sub_plain(m: re.Match) -> str:
        nonlocal count
        url = html.unescape(m.group(1))
        # 外部（note.com / rakusen.exblog.jp）を指すカードだけを対象にする。既に自サイトURLの
        # カードはCocoonの内部ブログカードとして正常に遷移できているため触らない。
        match = _match_target(url)
        if not match:
            return m.group(0)
        kind, key = match
        target = (note_map if kind == "note" else exblog_map).get(key)
        if not target:
            return m.group(0)  # サイト内に対応記事が無ければ元の外部カードを維持
        count += 1
        return build_article_card_html(target)

    new_content = BLOGCARD_SHORTCODE_PATTERN.sub(_sub_nested_p, content_html)
    new_content = BLOGCARD_SHORTCODE_H3_PATTERN.sub(_sub_h3, new_content)
    new_content = BLOGCARD_SHORTCODE_PLAIN_PATTERN.sub(_sub_plain, new_content)
    if new_content == content_html:
        return content_html, 0
    # 呼び出し元（backfill等）はcountが0だと「変更なし」として保存自体をスキップして
    # しまうため、実際のカード変換が0件でも、安全化のための正規化だけが行われた
    # 場合はcountを最低1にして「変更あり」を必ず呼び出し元に伝える。
    return new_content, max(count, 1)


def convert_internal_links(
    content_html: str,
    note_map: dict[str, dict],
    exblog_map: dict[str, dict],
) -> tuple[str, int]:
    """
    本文HTML中の note.com / rakusen.exblog.jp へのリンク（通常のテキストリンクと、
    noteの「ブログカード」埋め込みの両方）のうち、サイト内に対応記事が存在する
    ものだけを内部リンク（junchan-world.com）に置き換える。
    アンカー（#...）が付いている場合は変換後のURLにもそのまま引き継ぐ
    （note側の段落ID等はWP側と一致しないため必ずしもジャンプ先が一致しないが、
    一致しない場合はページ先頭に留まるだけで実害が無いため、安全側の措置として
    単純に引き継ぐ）。赤枠（サブスク案内ボックス）内のリンクは対象外とする。

    戻り値: (変換後のHTML, 置換件数)。置換が1件も無ければ元のHTMLをそのまま返す
    （BeautifulSoupの再シリアライズによる無意味な差分を避けるため）。
    """
    replaced = 0
    # Cocoonの[blogcard]ショートコード（壊れたネスト構造）は、BeautifulSoupで解析
    # する前に生文字列の状態で安全に置換しておく（理由は関数のdocstringを参照）。
    content_html, blogcard_count = replace_blogcard_shortcodes(content_html, note_map, exblog_map)
    replaced += blogcard_count

    has_external_ref = "note.com" in content_html or "exblog.jp" in content_html
    has_downgraded_card = "note-embed-link" in content_html  # 旧ロジックでの変換済みカード（要リッチカード化）
    if not has_external_ref and not has_downgraded_card:
        return (content_html, replaced) if replaced else (content_html, 0)

    soup = BeautifulSoup(content_html, "html.parser")

    # 先にブログカード（figure埋め込み）を処理する。カードごと置き換えるため、
    # カード内部に残っていた旧hrefを後段のaタグ走査で二重に処理する必要が無くなる。
    replaced += _replace_note_embed_cards(soup, note_map)
    # 旧ロジックで文字リンクまでダウングレードされてしまったカードをリッチカードへ戻す。
    replaced += _upgrade_simple_embed_links(soup, note_map)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = _match_target(href)
        if not match:
            continue
        kind, key = match
        target = (note_map if kind == "note" else exblog_map).get(key)
        if not target:
            continue  # サイト内に対応記事が無ければ元の外部リンクを維持
        if _is_in_red_box(a):
            continue  # 赤枠内の元記事確認・購入用リンクは絶対に変更しない

        fragment = urlparse(href).fragment
        new_href = target["link"] + (f"#{fragment}" if fragment else "")
        if new_href != href:
            a["href"] = new_href
            replaced += 1

    if replaced == 0:
        return content_html, 0
    return str(soup), replaced


# QAルール①（CLAUDE.md「5. 記事内リンクの一括変換・バックフィル作業の教訓」
# および「6. 品質保証（QA）ルール」を参照）: 本番反映前に、生成・置換した
# HTMLへサーバーエラー応答の文字列がそのまま紛れ込んでいないかを機械的に検査する。
# 507エラー混入事故（外部/自サイトのライブメタデータ取得失敗がそのままカードの
# タイトル・画像として保存されてしまった）を受けて追加。
ERROR_TEXT_MARKERS = (
    "Insufficient Storage",
    "507 Insufficient Storage",
    "404 Not Found",
    "500 Internal Server Error",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
)


def find_error_text_markers(content_html: str) -> list[str]:
    """本文HTML中に混入したサーバーエラー応答らしき文字列を検出する。
    本番反映前の最終チェックとして呼び出すこと（該当が1件でもあれば、
    その投稿への書き込みは中止し、人間の確認を仰ぐこと）。"""
    return [marker for marker in ERROR_TEXT_MARKERS if marker in content_html]
