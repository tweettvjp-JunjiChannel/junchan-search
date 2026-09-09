"""
E2E回帰テスト: note.comのハッシュタグ（#付き）がシャープ無しの正規タグと
分断・重複していた不具合の解消を検証する。

【背景】2026-09-09: 実機で「気象兵器」タグと「#気象兵器」タグ、
「人工台風」タグと「#人工台風」タグがそれぞれ別タグとして存在し、
本来同じ話題の記事が2つのタグへ分散していることが報告された。原因は
auto_sync_blogs.py が note.com APIのハッシュタグ文字列（例: "#気象兵器"）
をシャープ付きのまま wp.get_or_create_tag() へ渡しており、既存のシャープ
無しタグと一致判定されず常に別タグとして新規作成されていたため。

【対策】
1. backfill_normalize_hashtag_tags.py で既存の264件のシャープ付きタグを
   一括クレンジング（正規タグが既存の69件は投稿を付け替えて統合・削除、
   正規タグが無い195件はシャープを除去して改名）。
2. auto_sync_blogs.py に normalize_note_tag_name() を追加し、note.comの
   ハッシュタグを取り込む際に必ず先頭の#/＃と前後の空白を除去するように
   した（今後の同期で同じ分断が再発しない）。
3. クレンジング後、category_tags.json・サイドバーの記事カテゴリー
   ウィジェット（custom_html-4）・タグ一覧固定ページ（/tags/）を
   再生成・反映した。

シナリオ:
    1. 「/tag/気象兵器/」を開き、統合後の正規タグに紐づく記事一覧が
       正しく取得できることを確認する。
    2. ページ本文中に「#気象兵器」という分断されたシャープ付きタグ表記が
       一切残っていないことを確認する。
    3. サイドバーの「キーワードから探す」リンクから /tags/ ページへ遷移し、
       「気象兵器」は存在するが「#気象兵器」は存在しないことを確認する
       （正規タグへの統合が一覧側にも反映されていることの検証）。

実行方法:
    python test_e2e_hashtag_tag_normalization.py
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

SITE_URL = "https://junchan-world.com"
TAG_SLUG_PATH = "/tag/%E6%B0%97%E8%B1%A1%E5%85%B5%E5%99%A8/"  # 気象兵器
CANONICAL_KEYWORD = "気象兵器"
HASH_KEYWORD = "#気象兵器"


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("=" * 60)
        print("ステップ1: /tag/気象兵器/ に記事が正しく集約されていることを確認")
        print("=" * 60)
        tag_url = f"{SITE_URL}{TAG_SLUG_PATH}?_e2e={int(time.time() * 1000)}"
        resp = page.goto(tag_url, wait_until="load", timeout=30000)
        assert resp is not None and resp.status == 200, f"[FAIL] /tag/気象兵器/ の取得に失敗しました（status={resp.status if resp else None}）"

        page.wait_for_selector('article[id^="post-"]', timeout=15000)
        ids = page.evaluate("() => Array.from(document.querySelectorAll('article[id^=\"post-\"]')).map(a => a.id)")
        print(f"該当記事: {len(ids)}件 / {ids}")
        assert len(ids) > 0, "[FAIL] 正規タグ「気象兵器」に記事が1件も紐づいていません"
        print("[OK] 正規タグへ記事が集約されている")

        body_text = page.locator("body").inner_text()
        assert HASH_KEYWORD not in body_text, f"[FAIL] ページ内に「{HASH_KEYWORD}」の表記が残っています"
        print(f"[OK] ページ内に分断されたタグ表記「{HASH_KEYWORD}」は存在しない")

        print("\n" + "=" * 60)
        print("ステップ2: サイドバー経由でタグ一覧ページを確認")
        print("=" * 60)
        kw_link = page.locator("a.cat-acc-tagindex-link").first
        kw_link.wait_for(state="attached", timeout=10000)
        href = kw_link.get_attribute("href") or "/tags/"

        tags_url = f"{SITE_URL}{href}?_e2e={int(time.time() * 1000)}"
        page.goto(tags_url, wait_until="load", timeout=30000)
        page.wait_for_selector(".tag-index-page", timeout=15000)
        tag_index_text = page.locator(".tag-index-page").inner_text()

        assert CANONICAL_KEYWORD in tag_index_text, f"[FAIL] タグ一覧ページに正規タグ「{CANONICAL_KEYWORD}」が見つかりません"
        assert HASH_KEYWORD not in tag_index_text, f"[FAIL] タグ一覧ページに分断タグ「{HASH_KEYWORD}」がまだ残っています"
        print(f"[OK] タグ一覧ページに「{CANONICAL_KEYWORD}」のみが存在し、「{HASH_KEYWORD}」は存在しない")

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
