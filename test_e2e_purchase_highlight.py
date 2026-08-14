"""
E2E回帰テスト: 「購入済み」の個人状態が、一覧カード・詳細ページの両方で
🛒バッジの赤色ハイライトとして正しく描画されることを検証する。

【背景】2026-08-14: 「スキ（💖）」と同様に、購入済みの閲覧者には🛒バッジを
赤色点灯させてほしいという要望を受けて実装した。このサイトにはログイン
機能が無いため（既存の「スキ」機能と同じ制約。CLAUDE.md 10.項参照）、
「このブラウザが過去に買ったか」をサーバー側で直接判定する手段は無い。
唯一の手がかりは、記事詳細ページに埋め込まれたCodoc自身の購入ウィジェット
（.wp-block-codoc-codoc-block）が、そのブラウザに対して購入ボタンを
表示するかどうか（購入直後のセッション、またはCodocへのログイン状態
＝単体購入・月額サブスクの両方を含む、で判定される。実機調査で
ウィジェット内に「ログインして購入を復元」という導線があることを確認済み）。
このボタンが消えている＝購入済みとみなし、LocalStorage（nseb_purchased_posts）
にフラグを記録した上で、一覧・詳細の両方で赤色表示を行う
（note-style-engagement.php の checkCodocPurchaseState / effectivePurchased 参照）。

このテストは実際のCodoc購入フロー（決済）を再現するのではなく、
LocalStorageへ直接「購入済み」フラグを注入してこの表示ロジック単体を検証する
（ユーザー提案の「Cookie/LocalStorage/APIレスポンス等をシュミレート」に対応）。
対象記事は実際にCodocで購入テストが行われ、購入数が本物の1になっている
post_id=57150（【原爆のウソ特集】300円記事）を使う。

シナリオ:
    A) LocalStorageに購入済みフラグを注入したブラウザ：
       1. 詳細ページで🛒バッジが赤色ハイライト（is-purchased）かつ「1人が購入」。
       2. サイト内検索結果の一覧カードでも🛒バッジが赤色ハイライトかつカウント1。
    B) 何もフラグを持たない通常のブラウザ（対照実験）：
       同じ記事の🛒バッジが赤色ハイライトされていない（灰色のまま）ことを確認し、
       「全員に無条件で赤く見える」という誤ったフォールスポジティブが無いことを保証する。

実行方法:
    python test_e2e_purchase_highlight.py
"""

from __future__ import annotations

import html
import re
import sys
import time

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SITE_URL = "https://junchan-world.com"
POST_ID = 57150  # 【原爆のウソ特集】実際にCodocで購入済み(purchased_count=1)の記事
LOCALSTORAGE_INIT_SCRIPT = (
    "try { window.localStorage.setItem('nseb_purchased_posts', JSON.stringify([" + str(POST_ID) + "])); } catch (e) {}"
)


def clean_search_query(raw_title: str) -> str:
    text = html.unescape(raw_title)
    runs = re.findall(r"[0-9A-Za-zぁ-んァ-ヶ一-龠]+", text)
    for run in runs:
        if len(run) >= 6:
            return run[:14]
    return ("".join(runs) or text)[:14]


def parse_count(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def cache_bust() -> str:
    return str(int(time.time() * 1000))


# 「赤色になっているはずなのに実際は灰色に見える」という報告を受けて、
# クラス名の有無だけでなく実際にブラウザが計算した色（getComputedStyle）まで
# 検証する。CSSの詳細度の衝突や、そもそもクラスは付いているが別の場所で
# 色が打ち消されている、といった「クラスはあるが見た目は変わっていない」
# 類の不具合はクラス名チェックだけでは検出できないため。
PURCHASED_COLOR_RGB = "rgb(224, 36, 94)"  # #e0245e（スキのis-likedと同じ赤）
UNPURCHASED_COLOR_RGB = "rgb(58, 107, 201)"  # #3a6bc9（通常の購入数バッジの色）


def get_computed_color(locator) -> str:
    return locator.evaluate("el => window.getComputedStyle(el).color")


def main() -> None:
    from playwright.sync_api import sync_playwright

    r = requests.get(f"{SITE_URL}/wp-json/wp/v2/posts/{POST_ID}", params={"_fields": "link,title"}, timeout=20)
    r.raise_for_status()
    post = r.json()
    detail_url = post["link"]
    title = post["title"]["rendered"]
    query = clean_search_query(title)
    print(f"対象記事: post_id={POST_ID} title={title!r}")
    print(f"詳細URL: {detail_url}")
    print(f"検索クエリ: {query!r}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        print("\n" + "=" * 60)
        print("ケースA: 購入済みフラグを注入したブラウザ")
        print("=" * 60)
        ctx_a = browser.new_context()
        ctx_a.add_init_script(LOCALSTORAGE_INIT_SCRIPT)
        page_a = ctx_a.new_page()

        print("-- A1: 詳細ページ --")
        page_a.goto(detail_url + "?_e2e=" + cache_bust(), wait_until="load", timeout=30000)
        purchased_el = page_a.locator(".nseb-stat-purchased").first
        page_a.wait_for_function(
            "el => el && !el.classList.contains('nseb-skeleton')",
            arg=purchased_el.element_handle(),
            timeout=20000,
        )
        page_a.wait_for_function(
            "() => { var el = document.querySelector('.nseb-stat-purchased'); "
            "return !!el && el.classList.contains('is-purchased'); }",
            timeout=15000,
        )
        detail_class = purchased_el.get_attribute("class") or ""
        detail_text = purchased_el.inner_text().strip()
        detail_color = get_computed_color(purchased_el)
        print(f"詳細ページ購入バッジ: class={detail_class!r} text={detail_text!r} color={detail_color!r}")
        assert "is-purchased" in detail_class, "[FAIL] 詳細ページで購入済みバッジが赤色ハイライトされていません"
        assert "1" in detail_text and "購入" in detail_text, f"[FAIL] 購入数表示が不正: {detail_text!r}"
        assert detail_color == PURCHASED_COLOR_RGB, (
            f"[FAIL] 詳細ページの購入バッジの実際の描画色が赤になっていません: {detail_color!r} "
            f"(期待値: {PURCHASED_COLOR_RGB!r})"
        )
        print(f"[OK] 詳細ページで🛒バッジが赤色（{detail_color}）で描画され、「1人が購入」と表示されている")

        print("-- A2: 一覧（実際のトップページ）ページ --")
        # ユーザー報告と全く同じページ（トップページの一覧カード）で検証する。
        # post_idはトップページの最新記事（1番上のカード）であることを別途確認済み。
        top_url = f"{SITE_URL}/?_e2e={cache_bust()}"
        page_a.goto(top_url, wait_until="load", timeout=30000)
        card_purchased_selector = f"article#post-{POST_ID} .nseb-card-purchased"
        page_a.wait_for_selector(card_purchased_selector, timeout=15000)
        page_a.wait_for_function(
            "sel => { var el = document.querySelector(sel); return !!el && el.classList.contains('is-purchased'); }",
            arg=card_purchased_selector,
            timeout=15000,
        )
        card_badge = page_a.locator(card_purchased_selector).first
        card_class = card_badge.get_attribute("class") or ""
        card_text = card_badge.inner_text().strip()
        card_color = get_computed_color(card_badge)
        card_bg = card_badge.evaluate("el => window.getComputedStyle(el).backgroundColor")
        print(f"一覧カード購入バッジ: class={card_class!r} text={card_text!r} color={card_color!r} bg={card_bg!r}")
        assert "is-purchased" in card_class, "[FAIL] 一覧カードで購入済みバッジが赤色ハイライトされていません"
        assert parse_count(card_text) >= 1, f"[FAIL] 一覧カードの購入数が1以上になっていません: {card_text!r}"
        assert card_color == PURCHASED_COLOR_RGB, (
            f"[FAIL] 一覧カードの購入バッジの実際の描画色が赤になっていません: {card_color!r} "
            f"(期待値: {PURCHASED_COLOR_RGB!r})"
        )
        print(f"[OK] 一覧カードで🛒バッジが赤色（{card_color}）で描画され、カウント1以上で表示されている")
        ctx_a.close()

        print("\n" + "=" * 60)
        print("ケースB: 何もフラグを持たない通常のブラウザ（対照実験）")
        print("=" * 60)
        ctx_b = browser.new_context()
        page_b = ctx_b.new_page()
        page_b.goto(top_url, wait_until="load", timeout=30000)
        page_b.wait_for_selector(card_purchased_selector, timeout=15000)
        # 赤色ハイライトが付かないことを確認するため、is-purchasedが付与されない
        # ことを一定時間安定して確認する（非同期描画完了を待つため少し待機）。
        page_b.wait_for_timeout(2500)
        card_badge_b = page_b.locator(card_purchased_selector).first
        card_class_b = card_badge_b.get_attribute("class") or ""
        card_text_b = card_badge_b.inner_text().strip()
        card_color_b = get_computed_color(card_badge_b)
        print(f"通常ブラウザの一覧カード購入バッジ: class={card_class_b!r} text={card_text_b!r} color={card_color_b!r}")
        assert "is-purchased" not in card_class_b, (
            "[FAIL] 購入済みフラグを持たない通常ブラウザでも赤色ハイライトされています"
            "（誤って全員に購入済み表示が出るフォールスポジティブ）"
        )
        assert card_color_b != PURCHASED_COLOR_RGB, (
            f"[FAIL] 通常ブラウザなのに実際の描画色が赤になっています: {card_color_b!r}"
        )
        print(f"[OK] 通常ブラウザでは赤色ハイライトされない（実際の描画色={card_color_b}、購入数自体は表示される）")
        ctx_b.close()

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
