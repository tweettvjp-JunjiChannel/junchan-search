"""
E2E回帰テスト: 検索実行後にサイドバーの検索入力欄が空欄にリセットされる
不具合の修正を検証する。

【背景】2026-09-09: 実機で「大谷翔平」と検索すると、メイン画面には
検索結果が正しく表示されるものの、サイドバーの検索入力欄（custom_html-3
ウィジェット、input[name="s"]）の中身が空欄に戻って見える不具合が
報告された。原因は、この入力欄がサイドバーウィジェット内に静的HTMLとして
value="" 固定で書かれていたこと。検索フォームの送信はPJAX対象外の通常の
GET遷移のため、送信のたびにこのウィジェットのHTML・scriptタグごと
サーバーから再取得・再実行されるが、従来のスクリプトはURLの ?s= を一切
読んでおらず、常に空文字のまま描画されていた。

【対策】ウィジェット（custom_html-3）自身のスクリプトに、読み込み時に
URLパラメータ s を読んで入力欄へ反映する処理と、値がある場合にクリア
ボタン（×）をアクティブ表示する処理を追加した。あわせて、PJAX遷移
（history.pushState / popstate）にも追従できるよう、プラグイン本体
（custom-search-filter.php）を一切変更せず、ウィジェット自身が
window.history.pushState をラップし、popstateイベントも購読することで
単独で同期する設計にした（オーナーの明示的な指示により、プラグイン本体の
plugin-editor.php経由デプロイは行わずウィジェットのREST API経由デプロイ
のみで完結させている）。

シナリオ:
    1. 「/?s=<大谷翔平のURLエンコード>&filter_submitted=1&filter_cats[]=note」
       を新規（Cookie/LocalStorage空）ブラウザコンテキストで開く。
    2. サイドバー検索入力欄の inputValue() が厳密に「大谷翔平」であること、
       およびクリアボタンが表示状態（is-visible クラス付与）であることを
       アサートする。
    3. history.pushState経由のURL変更（PJAX遷移を模したもの）でも入力欄が
       追従して再同期されること、popstate（戻る）でも同様に追従することを
       あわせて確認する。

実行方法:
    python test_e2e_sidebar_search_input_restore.py
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SITE_URL = "https://junchan-world.com"
# 「大谷翔平」の正しいUTF-8パーセントエンコード。
# 大=%E5%A4%A7 谷=%E8%B0%B7 翔=%E7%BF%94 平=%E5%B9%B3
SEARCH_QUERY_ENCODED = "%E5%A4%A7%E8%B0%B7%E7%BF%94%E5%B9%B3"
EXPECTED_SEARCH_TERM = "大谷翔平"


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        print("=" * 60)
        print("ステップ1: 検索結果ページ（?s=大谷翔平）を新規コンテキストで開く")
        print("=" * 60)
        ctx = browser.new_context()
        page = ctx.new_page()

        url = (
            f"{SITE_URL}/?s={SEARCH_QUERY_ENCODED}"
            "&filter_submitted=1&filter_cats%5B%5D=note"
            f"&_e2e={int(time.time() * 1000)}"
        )
        resp = page.goto(url, wait_until="load", timeout=30000)
        print(f"HTTPステータス: {resp.status if resp else 'N/A'}")
        assert resp is not None and resp.status == 200, "[FAIL] 検索結果ページの取得に失敗しました"

        print("\n" + "=" * 60)
        print("ステップ2: サイドバー検索入力欄の値とクリアボタンの表示状態を確認")
        print("=" * 60)
        input_locator = page.locator('.custom-search-filter-form input[name="s"]').first
        input_locator.wait_for(state="attached", timeout=10000)
        value = input_locator.input_value()
        print(f"検索入力欄の値: {value!r}")
        assert value == EXPECTED_SEARCH_TERM, (
            f"[FAIL] サイドバーの検索入力欄にURLの検索語が反映されていません: {value!r}"
        )
        print("[OK] 検索入力欄の値がURLの s パラメータと厳密に一致している")

        clear_btn = page.locator(".nseb-search-clear-btn").first
        clear_btn.wait_for(state="attached", timeout=5000)
        is_visible = clear_btn.is_visible()
        classes = clear_btn.get_attribute("class") or ""
        print(f"クリアボタン表示状態: visible={is_visible} / class={classes!r}")
        assert is_visible, "[FAIL] 検索語がある状態でクリアボタンが表示されていません"
        assert "is-visible" in classes.split(), "[FAIL] クリアボタンに is-visible クラスが付与されていません"
        print("[OK] クリアボタンがアクティブ表示されている")

        print("\n" + "=" * 60)
        print("ステップ3: PJAX遷移相当（pushState）・戻る操作（popstate）への追従を確認")
        print("=" * 60)
        page.evaluate(
            "() => { window.history.pushState({nsebPjax:true}, '', "
            "'/?s=%E9%80%86%E9%A2%A8&filter_submitted=1'); }"
        )
        page.wait_for_timeout(200)
        value_after_push = input_locator.input_value()
        print(f"pushState後の入力欄の値: {value_after_push!r}")
        assert value_after_push == "逆風", (
            f"[FAIL] history.pushState によるURL変更に入力欄が追従していません: {value_after_push!r}"
        )
        print("[OK] pushState（PJAX遷移相当）でも入力欄が再同期される")

        page.go_back()
        page.wait_for_timeout(300)
        value_after_back = input_locator.input_value()
        print(f"go_back（popstate）後の入力欄の値: {value_after_back!r}")
        assert value_after_back == EXPECTED_SEARCH_TERM, (
            f"[FAIL] popstate（戻る操作）に入力欄が追従していません: {value_after_back!r}"
        )
        print("[OK] popstate（戻る操作）でも入力欄が再同期される")

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
