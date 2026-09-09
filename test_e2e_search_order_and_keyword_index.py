"""
E2E回帰テスト: 検索結果の並び順（タイトル一致優先＋同順位内は新着順）と、
サイドバー「キーワードから探す（50音順）」への最新タグ反映を検証する。

【背景】2026-09-09: 実機で「気象兵器」と検索したところ、表示順が
1) 2025.08.17 → 2) 2024.09.01 → 3) 2026.08.29 と、最も新しい記事が
一番下に沈む不具合が報告された。custom-search-filter.php の
prioritize_title_matches()（タイトル一致記事を絶対優先し、同順位内は
post_date DESC）自体は本番へ正しくデプロイ済みで、SQLロジックとしては
問題がないことを確認した。真因は auto_sync_blogs.py の sync_note_updates()
にあった：note記事の変更検知が本文HTMLのハッシュのみに基づいており、
note.com側でタイトルやハッシュタグ（タグ）だけを編集し本文自体は変えて
いないケース（例: nac32945bc0e8 のタイトルへの「【気象兵器】」追記）を
「変更なし」として黙って無視していたため、対象記事の note_full_title
メタが古いまま（＝タイトル一致の判定対象に入らない）になり、タイトル一致
優先の並び替えロジックそのものは正しくても入力データが古いままだった。

あわせて、sync_note_updates() は新規投稿時（sync_new_note_posts）と異なり
タグの同期を一切行っていなかったため、note.com側で新しいハッシュタグ
（#気象兵器 等）が付いても、既存記事の更新経路ではWordPress側のタグに
反映されず、サイドバーの「キーワードから探す（50音順）」（/tags/ 固定
ページ、generate_tag_index.py で生成）にも永遠に載らない不具合があった。

【対策（auto_sync_blogs.py）】
1. 変更検知のハッシュ対象を本文HTMLだけでなく「タイトル＋タグ＋本文」に
   拡張（state内のキーを body_hash → content_hash に変更）。
2. sync_note_updates() でも新規投稿時と同様にタグを毎回同期するようにした。
3. 30日ウィンドウ外・ハッシュ不変でも特定記事を強制的に即時更新できる
   --key オプションを追加。
4. --execute実行の最後に、generate_tag_index.py のロジックを呼び出し、
   タグ一覧固定ページ（/tags/）を自動再生成・反映するようにした
   （今後は同期のたびに新しいキーワードが自動でサイドバーへ反映される）。

シナリオ:
    1. 「/?s=気象兵器&filter_submitted=1&filter_cats[]=note」を開き、
       検索結果1件目の日付（.entry-date）が2026年であることを確認する
       （タイトル一致優先ソートの同順位内で最新記事が先頭に来ることの検証）。
    2. サイドバーの「🔤 キーワードから探す（50音順）」リンク（/tags/）へ
       遷移し、そのページ内に「気象兵器」というキーワードが存在することを
       確認する（note.comの新しいハッシュタグが同期の度に自動反映される
       ことの検証）。

実行方法:
    python test_e2e_search_order_and_keyword_index.py
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SITE_URL = "https://junchan-world.com"
# 「気象兵器」の正しいUTF-8パーセントエンコード。
SEARCH_QUERY_ENCODED = "%E6%B0%97%E8%B1%A1%E5%85%B5%E5%99%A8"
EXPECTED_KEYWORD = "気象兵器"


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("=" * 60)
        print("ステップ1: 検索結果1件目が2026年の記事であることを確認")
        print("=" * 60)
        search_url = (
            f"{SITE_URL}/?s={SEARCH_QUERY_ENCODED}"
            "&filter_submitted=1&filter_cats%5B%5D=note"
            f"&_e2e={int(time.time() * 1000)}"
        )
        resp = page.goto(search_url, wait_until="load", timeout=30000)
        assert resp is not None and resp.status == 200, "[FAIL] 検索結果ページの取得に失敗しました"

        page.wait_for_selector('article[id^="post-"]', timeout=15000)
        first_article = page.locator('article[id^="post-"]').first
        first_id = first_article.get_attribute("id")
        first_date = first_article.locator(".entry-date").first.inner_text()
        print(f"検索結果1件目: {first_id} / 日付: {first_date}")
        assert first_date.startswith("2026"), (
            f"[FAIL] 検索結果1件目が2026年の記事になっていません（日付: {first_date}）"
        )
        print("[OK] 検索結果1件目が2026年の記事になっている")

        print("\n" + "=" * 60)
        print("ステップ2: サイドバーのキーワードリンクから /tags/ へ遷移し「気象兵器」を確認")
        print("=" * 60)
        kw_link = page.locator("a.cat-acc-tagindex-link").first
        kw_link.wait_for(state="attached", timeout=10000)
        href = kw_link.get_attribute("href") or ""
        print(f"「キーワードから探す」リンク先: {href}")
        assert "/tags/" in href, f"[FAIL] キーワードリンクの遷移先が想定と異なります: {href}"

        tags_url = f"{SITE_URL}/tags/?_e2e={int(time.time() * 1000)}"
        page.goto(tags_url, wait_until="load", timeout=30000)
        page.wait_for_selector(".tag-index-page", timeout=15000)
        body_text = page.locator(".tag-index-page").inner_text()
        found = EXPECTED_KEYWORD in body_text
        print(f"タグ一覧ページに「{EXPECTED_KEYWORD}」が含まれる: {found}")
        assert found, f"[FAIL] サイドバーのキーワードリスト（/tags/）に「{EXPECTED_KEYWORD}」が存在しません"
        print(f"[OK] サイドバーのキーワードリストに「{EXPECTED_KEYWORD}」が存在する")

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
