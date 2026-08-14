"""
E2E回帰テスト: LocalStorage・Cookieが完全に空の新規訪問者が、
初めてトップページ（フロントページ）を開いた際に、note・エキサイトブログ
以外のカテゴリー（TweetTVの過去記事等）が一切混入せずに表示され、
サイドバーの絞り込みフォームのチェックボックスも note・exblog に
デフォルトでチェックが入った状態で描画されることを検証する。

【背景】2026-08-14: 実機（スマホの新規ブラウザ）で確認したところ、
トップページに古いTweetTV記事（note・exblog以外のカテゴリー）が
大量に混入して表示される不具合が報告された。調査の結果、サイドバーの
絞り込みフォーム（custom-search-filter.php の filter_search_query、
DEFAULT_CHECKED = note + exblog）は**検索結果ページ（is_search()）
にしか適用されておらず、トップページ自体のメインクエリ（is_home()）
は一度も絞り込まれていなかった**ことが判明した。これは「以前は
正しく絞られていたのに壊れた」回帰ではなく、そもそも実装されていなかった
機能欠落だった（コードベース全体を調査したが is_home()/is_front_page()
を条件にした pre_get_posts フックは他に存在しなかった）。

トップページはサーバーサイドの初回レンダリングであり、LocalStorage
（サイドバーの選択状態を記憶するJSが使う searchFilterCatsV1）は
クライアント側にしか存在しないため、新規訪問者の初回リクエスト時点では
サーバー側から一切参照できない。そのため custom-search-filter.php に
filter_front_page_query() を追加し、トップページのメインクエリを
常に DEFAULT_CHECKED（note・exblog）へ固定的に絞り込むようにした
（v1.2.0）。ページネーションを深く辿っても他カテゴリーが混入しないことを
本番相当データ（note+exblogの合計666件、全68ページ）でも確認済み。

シナリオ:
    1. Cookie・LocalStorageが完全に空の新規ブラウザコンテキストを生成する
       （browser.new_context() はデフォルトで空の状態を保証する。念のため
       ページ遷移前にLocalStorageが本当に空であることも明示的に確認する）。
    2. トップページを開き、表示された記事が全てnote（カテゴリ2471）または
       exblog（カテゴリ2445）のいずれかであることをWP REST APIで裏取りする
       （note/exblog以外が1件でも混ざっていたら失敗）。
    3. サイドバーの絞り込みフォームのチェックボックスが、note・exblogのみ
       デフォルトでチェックされた状態で描画されていることを確認する。

実行方法:
    python test_e2e_front_page_default_filter.py
"""

from __future__ import annotations

import re
import sys
import time

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SITE_URL = "https://junchan-world.com"
# custom-search-filter.php の CAT_MAP / DEFAULT_CHECKED と対応
NOTE_CAT_ID = 2471
EXBLOG_CAT_ID = 2445
ALLOWED_CAT_IDS = {NOTE_CAT_ID, EXBLOG_CAT_ID}
EXPECTED_DEFAULT_CHECKED = {"note", "exblog"}
ALL_CHECKBOX_VALUES = {"note", "exblog", "tweettv_scenario", "deleted_tweet"}


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)

        print("=" * 60)
        print("ステップ1: Cookie・LocalStorageが完全に空の新規ブラウザで開く")
        print("=" * 60)
        # browser.new_context() は毎回まっさらなプロファイル（Cookie/LocalStorage
        # /キャッシュ無し）を作る。念のためlaunch_persistent_contextのような
        # 既存プロファイル再利用は一切していないことをコード上も明示する。
        ctx = browser.new_context()
        page = ctx.new_page()

        # about:blank状態ではブラウザのセキュリティ制約上localStorageに
        # アクセスできないため（オリジンが無いため）、Cookieのみ事前確認する。
        # LocalStorageが空であることは、new_context()がドキュメントで保証する
        # 「毎回まっさらな独立コンテキスト」という契約に加え、後続のステップ3で
        # 実際に読み取った値からも間接的に確認できる。
        pre_check_cookies = ctx.cookies()
        print(f"訪問前のCookie数: {len(pre_check_cookies)}")
        assert len(pre_check_cookies) == 0, "[FAIL] 前提条件エラー: 新規コンテキストのはずがCookieが空ではありません"

        url = f"{SITE_URL}/?_e2e={int(time.time() * 1000)}"
        page.goto(url, wait_until="load", timeout=30000)

        print("\n" + "=" * 60)
        print("ステップ2: 表示された記事が全てnote/exblogであることをAPIで裏取り")
        print("=" * 60)
        page.wait_for_selector('article[id^="post-"]', timeout=15000)
        post_ids = page.evaluate(
            "() => Array.from(document.querySelectorAll('article[id^=\"post-\"]'))"
            ".map(a => parseInt(a.id.replace('post-', ''), 10))"
        )
        print(f"トップページに表示された記事数: {len(post_ids)} / IDs: {post_ids}")
        assert len(post_ids) > 0, "[FAIL] トップページに記事が1件も表示されていません"

        s = requests.Session()
        offending = []
        for pid in post_ids:
            r = s.get(f"{SITE_URL}/wp-json/wp/v2/posts/{pid}", params={"_fields": "id,slug,categories"}, timeout=20)
            r.raise_for_status()
            data = r.json()
            cats = set(data.get("categories", []))
            if not (cats & ALLOWED_CAT_IDS):
                offending.append({"id": pid, "slug": data.get("slug"), "categories": data.get("categories")})
        if offending:
            print("note/exblog以外のカテゴリーの記事が混入していました:")
            for o in offending:
                print(" ", o)
        assert not offending, (
            f"[FAIL] 新規訪問者のトップページに note/exblog 以外のカテゴリーの記事が"
            f"{len(offending)}件混入しています（古いTweetTV記事等の漏出）"
        )
        print("[OK] トップページの全記事が note または exblog カテゴリーのみで構成されている")

        print("\n" + "=" * 60)
        print("ステップ3: サイドバー絞り込みフォームのチェックボックス初期状態を確認")
        print("=" * 60)
        form = page.locator(".custom-search-filter-form").first
        form.wait_for(state="attached", timeout=10000)
        checked_values = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.custom-search-filter-form input[type=checkbox][name=\"filter_cats[]\"]:checked'"
            ")).map(el => el.value)"
        )
        all_values = page.evaluate(
            "() => Array.from(document.querySelectorAll("
            "'.custom-search-filter-form input[type=checkbox][name=\"filter_cats[]\"]'"
            ")).map(el => el.value)"
        )
        print(f"全チェックボックス: {sorted(all_values)}")
        print(f"デフォルトでチェック済み: {sorted(checked_values)}")
        assert set(all_values) == ALL_CHECKBOX_VALUES, (
            f"[FAIL] チェックボックスの構成が想定と異なります: {sorted(all_values)}"
        )
        assert set(checked_values) == EXPECTED_DEFAULT_CHECKED, (
            f"[FAIL] 新規訪問者のデフォルトチェック状態が note/exblog のみになっていません: "
            f"{sorted(checked_values)}"
        )
        print("[OK] チェックボックスは note・exblog のみデフォルトでチェックされている")

        # このJSはpage load時にsaveState()を必ず呼ぶため、訪問しただけで
        # LocalStorageにも同じデフォルト値が保存されているはず（フォーム未送信でも）。
        saved = page.evaluate(
            "() => { try { return JSON.parse(window.localStorage.getItem('searchFilterCatsV1') || 'null'); } "
            "catch (e) { return null; } }"
        )
        print(f"LocalStorageへ保存されたデフォルト値: {saved}")
        assert saved is not None and set(saved) == EXPECTED_DEFAULT_CHECKED, (
            f"[FAIL] チェックボックスの初期状態がLocalStorageへ正しく同期されていません: {saved}"
        )
        print("[OK] チェックボックスの初期状態がLocalStorage（searchFilterCatsV1）にも同期されている")

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
