"""
note本文変換（目次・X埋め込み）の回帰テスト。WordPressへの書き込みは一切行わない。

【背景（2026-10-11）】note.comのフロントエンド刷新で、(1) 目次が
<nav class="o-tableOfContents"> から <div role="presentation"><nav aria-label="目次">
<details>...「すべて表示」...</details></nav></div> に、(2) X埋め込みが描画済みiframeから
描画前の <blockquote data-class="twitter-tweet"> に変わった。変換処理がこの変化に
追従できず、直近90日同期で61件が未整形のまま上書きされる事故が起きたため、
実際の崩れたHTML（post 1019 のリビジョン）をフィクスチャとして固定化している。

実行方法:
    python test_note_body_conversion.py          # フィクスチャによるオフラインテストのみ
    python test_note_body_conversion.py --live   # 加えてnote.comから実記事を取得して検証（WP書き込みなし）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from bs4 import BeautifulSoup

import auto_sync_blogs as core

FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"
LIVE_NOTE_URLS = [
    # 目次とX埋め込みの両方を含む記事（post 1019、2026-10-11の事故の対象）
    "https://note.com/tweettv/n/n87b282e893b4",
    # X埋め込み11件を含む記事
    "https://note.com/tweettv/n/n410f06716ce4",
]


def assert_toc_converted(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    assert "すべて表示" not in html, "note生の「すべて表示」ボタンが残っている"
    assert "o-tableOfContents" not in html
    assert soup.find("nav", attrs={"aria-label": "目次", "class": "mt-9"}) is None, "note生の目次navが残っている"
    assert soup.find("div", attrs={"role": "presentation"}) is None, "目次の包みdivが残っている"
    nav = soup.find("nav", class_="toc")
    assert nav is not None, "独自の目次（nav.toc）が生成されていない"
    items = nav.select("ul.toc-list > li.toc-item > a")
    assert items, "目次項目が0件"
    for a in items:
        target = a["href"].lstrip("#")
        assert soup.find(id=target) is not None, f"目次リンク先の見出しが存在しない: {a['href']}"
        assert a.get_text(strip=True) != "目次", "nav内の「目次」見出し自体が項目に混入している"
    # 案内文「⬇目次の読みたい項目…⬇」の直後に独自目次が来ていること
    guide = soup.find(string=lambda s: s and "目次の読みたい項目" in s)
    if guide is not None:
        assert guide.find_next("nav") is nav, "案内文の直後に独自目次が配置されていない"
    return len(items)


def assert_x_converted(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    figures = soup.find_all("figure", attrs={"embedded-service": "twitter"})
    assert figures, "X埋め込みのfigureが見つからない"
    for fig in figures:
        quote = fig.find("blockquote")
        assert quote is not None and "twitter-tweet" in (quote.get("class") or []), \
            "blockquoteにclass=twitter-tweetが無い（widgets.jsがカード化できない）"
        assert not quote.has_attr("data-class")
        assert quote.find("a", href=lambda h: h and "/status/" in h), "投稿URLへのリンクが無い"
        script = fig.find("script")
        assert script is not None and script.get("src") == core.TWITTER_WIDGETS_SCRIPT_SRC, "widgets.jsが無い"
        assert fig.find("iframe") is None, "高さ固定の旧iframeが残っている"
        assert "sessionId=" not in str(fig)
    return len(figures)


def convert(html: str) -> str:
    html = core.normalize_twitter_embeds(html)
    return core.rebuild_note_toc(html)


def test_new_note_dom_fixture() -> None:
    html = (FIXTURE_DIR / "note_body_2026_10_toc_and_x.html").read_text(encoding="utf-8")
    assert "すべて表示" in html and 'data-class="twitter-tweet"' in html  # 前提：変換前は崩れている
    out = convert(html)
    n_toc = assert_toc_converted(out)
    n_x = assert_x_converted(out)
    assert "皇室典範について話すTVの解説者" in out, "ツイート本文（フォールバック表示用）が失われた"
    assert convert(out) == out, "変換が冪等でない（再同期のたびに差分ありと誤判定される）"
    print(f"  [OK] 新DOM: 目次{n_toc}項目を再構築 / X埋め込み{n_x}件を正規化 / 冪等")


def test_legacy_rendered_x_fixture() -> None:
    html = (FIXTURE_DIR / "note_body_legacy_x_rendered.html").read_text(encoding="utf-8")
    out = convert(html)
    assert_x_converted(out)
    assert "2076293127988769188" in out
    print("  [OK] 旧DOM（描画済みiframe）のX埋め込みも標準形式へ正規化")


def test_legacy_toc_still_supported() -> None:
    html = (
        '<nav class="o-tableOfContents" data-v-1><h2>目次</h2><ul><li><a href="#a">見出しA</a></li></ul></nav>'
        '<h2 id="a">見出しA</h2><h3 id="b">見出しB</h3>'
    )
    out = core.rebuild_note_toc(html)
    soup = BeautifulSoup(out, "html.parser")
    assert soup.find("nav", class_="o-tableOfContents") is None
    texts = [a.get_text() for a in soup.select("nav.toc a")]
    assert texts == ["見出しA", "見出しB"], texts
    print("  [OK] 旧目次（o-tableOfContents）も従来どおり変換")


def test_already_converted_toc_untouched() -> None:
    html = '<nav aria-label="目次" class="toc"><ul class="toc-list"></ul></nav><h2 id="a">A</h2>'
    assert core.rebuild_note_toc(html) == html
    print("  [OK] 変換済みの独自目次（nav.toc）には触れない")


def test_code_block_copy_button_removed() -> None:
    html = (
        '<div class="group relative"><pre><code data-name="code">print(1)</code></pre>'
        '<div class="absolute right-2 top-2"><button data-id="ButtonIcon" type="button">'
        '<span aria-hidden="true"></span><svg><path d="M0"></path></svg></button></div></div>'
        '<p>本文</p>'
    )
    out = core.strip_note_ui_buttons(html)
    assert "<button" not in out and "<svg" not in out, out
    assert "<pre><code" in out and "print(1)" in out and "<p>本文</p>" in out, out
    assert 'class="absolute right-2 top-2"' not in out, "ボタンを包む空divが残っている"
    print("  [OK] コードブロックのコピーボタン（note UI部品）を除去し、コード本体は維持")


EXBLOG_MAP = {
    "22711505": {"id": 50166, "slug": "exblog-22711505", "link": "https://junchan-world.com/exblog-22711505/",
                 "title": "エキサイトブログ移行記事", "thumbnail": "https://junchan-world.com/wp-content/uploads/x.jpg"},
}
NOTE_MAP = {
    "n84fb8cb4cfaf": {"id": 1, "slug": "n84fb8cb4cfaf", "link": "https://junchan-world.com/n84fb8cb4cfaf/",
                      "title": "note移行記事", "thumbnail": ""},
}


def run_link_pipeline(body_html: str) -> str:
    """本番（sync_note_updates / sync_new_note_posts）と同じ順序でリンク系の変換を通す。"""
    html = core.convert_external_article_embeds_to_blogcards(body_html)
    html, _ = core.convert_internal_links(html, NOTE_MAP, EXBLOG_MAP)
    return core.finalize_external_blogcard_urls(html)


def test_own_exblog_external_card_becomes_internal_link() -> None:
    # 2026-10-11の事故：note記事内に外部カードとして貼られた自分のエキサイトブログ記事が
    # 外部URL（403で開けないrakusen.exblog.jp）のまま残っていた
    body = (
        '<figure embedded-service="external-article" data-src="https://rakusen.exblog.jp/22711505/">'
        '<a href="https://rakusen.exblog.jp/22711505/"><div class="external-article-widget-title">徳川のルーツ</div>'
        '<div class="external-article-widget-description">説明文</div></a></figure>'
        '<figure embedded-service="external-article" data-src="https://note.com/tweettv/n/n84fb8cb4cfaf">'
        '<a href="https://note.com/tweettv/n/n84fb8cb4cfaf"><div class="external-article-widget-title">別班</div></a></figure>'
    )
    out = run_link_pipeline(body)
    soup = BeautifulSoup(out, "html.parser")
    links = [a["href"] for a in soup.find_all("a", href=True)]
    assert links == ["https://junchan-world.com/exblog-22711505/", "https://junchan-world.com/n84fb8cb4cfaf/"], links
    assert "rakusen.exblog.jp" not in out and "note.com/tweettv" not in out, out
    assert "[blogcard" not in out, "外部ブログカードのショートコードが残っている"
    for a in soup.find_all("a", href=True):
        assert "internal-article-card" in (a.get("class") or []), "カード全体が<a>で囲まれた内部カードになっていない"
        assert a.get_text(strip=True), "カードにタイトルが無い"
    print("  [OK] 自サイトのexblog/note記事への外部カード → hrefを持つ内部カード<a>に変換")


def test_other_external_cards_keep_url() -> None:
    body = (
        '<figure embedded-service="external-article" data-src="https://ja.wikipedia.org/wiki/X">'
        '<a href="https://ja.wikipedia.org/wiki/X"><div class="external-article-widget-title">X - Wikipedia</div></a></figure>'
        '<figure embedded-service="external-article" data-src="https://rakusen.exblog.jp/99999999/">'
        '<a href="https://rakusen.exblog.jp/99999999/"><div class="external-article-widget-title">未移行</div></a></figure>'
        '<p>[blogcard url="https://junchan-world.com/exblog-28299681/?title=A"]</p>'
    )
    out = run_link_pipeline(body)
    assert '[blogcard url="https://ja.wikipedia.org/wiki/X?title=X+-+Wikipedia"]' in out, out
    assert '[blogcard url="https://rakusen.exblog.jp/99999999/?title=' in out, "対応記事の無いexblogカードは元のURLを維持すべき"
    assert '[blogcard url="https://junchan-world.com/exblog-28299681/?title=A"]' in out, "既に内部URLのカードは触らない"
    print("  [OK] 他サイト・未移行exblog・既に内部URLのカードは元のURLのまま維持")


def test_live_fetch() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context, page = core.get_note_browser_page(pw, headless=True)
        try:
            for url in LIVE_NOTE_URLS:
                html = core.fetch_note_body_html(page, url)
                n_toc = assert_toc_converted(html)
                n_x = assert_x_converted(html)
                assert "<button" not in html, "noteのUIボタンが残っている"
                markers = core.find_error_text_markers(html)
                assert not markers, f"エラー文字列が混入: {markers}"
                again = core.fetch_note_body_html(page, url)
                same = core.body_hash(html) == core.body_hash(again)
                assert same, "同じ記事を2回取得してハッシュが一致しない（差分ありの誤判定が起きる）"
                print(f"  [OK] 実取得 {url}: 目次{n_toc}項目 / X埋め込み{n_x}件 / 2回取得でハッシュ一致")
        finally:
            context.close()


def main() -> None:
    tests = [
        test_new_note_dom_fixture,
        test_legacy_rendered_x_fixture,
        test_legacy_toc_still_supported,
        test_already_converted_toc_untouched,
        test_code_block_copy_button_removed,
        test_own_exblog_external_card_becomes_internal_link,
        test_other_external_cards_keep_url,
    ]
    if "--live" in sys.argv:
        tests.append(test_live_fetch)
    failed = 0
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  [失敗] {e}")
    print(f"\n結果: {len(tests) - failed}/{len(tests)} 件成功")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
