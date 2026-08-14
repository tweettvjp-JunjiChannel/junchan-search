"""
E2E回帰テスト: Codocの購入数（🛒バッジ）が詳細ページ・一覧ページの両方に
正しく、かつ矛盾なく表示されることを検証する。

【背景】2026-08-14: 「Codocで実際に購入しても詳細ページのバッジが
『🛒 0人が購入』のまま固定される」「一覧カードには価格・PV・スキしかなく
購入バッジ自体が出ない」という2件の不具合報告を受けて調査した。

  - 詳細ページ側は元々 codoc_cached_purchased_count postmeta（WP-Cronで
    1日2回だけ更新）を読んでいたため、決済直後は反映に最大12時間かかる
    設計だった。Codocダッシュボードを実機調査したが、決済完了を能動的に
    通知するWebhookは存在しないため、真のリアルタイム連動は不可能。
    代替として、単一記事ページを開くたびにその1記事分だけキャッシュを
    ライブ更新するようにした（handle_view内のmaybe_live_refresh_codoc_cache_for_post。
    直近300秒以内に更新済みならスキップするレート制限つき）。
  - 一覧カード側は購入数が1以上の場合のみバッジを描画しており、
    「有料記事だが購入者0人」の記事は詳細ページには表示があるのに一覧には
    何も出ない、という矛盾があったため、購入数が判明している（null出ない）
    限り0人でも表示するよう統一した（renderCardBadges）。

このテストは実在する有料記事（codoc_entry_codeを持つ記事）を1件選び、
postmetaの購入数を一時的に「1」へ書き換えた状態で、
    1) 詳細ページに「🛒 1人が購入」と表示されること
    2) 同じ記事のカードが一覧（サイト内検索結果）にも表示され、
       「🛒1」バッジが出ていること
を確認する。postmetaの直接書き換えでテストできるのは、
maybe_live_refresh_codoc_cache_for_post のレート制限（直近300秒以内は
ライブ再取得をスキップする）により、cache_updated_atを「今」にセットして
おけば注入した値がCodocへの実アクセスで上書きされないため。

実行方法:
    python test_e2e_purchase_count_sync.py
"""

from __future__ import annotations

import html
import json
import re
import sys
import time

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

with open("wp_credentials.json", encoding="utf-8") as f:
    CREDS = json.load(f)
SITE_URL = CREDS["site_url"].rstrip("/")
AUTH = (CREDS["username"], CREDS["application_password"])

NOTE_SLUG_PATTERN = re.compile(r"^n[0-9a-f]{12}(-\d+)?$")
CACHE_BUST = lambda: str(int(time.time() * 1000))  # noqa: E731


def find_paid_note_post() -> dict:
    """codoc_entry_codeを持つ（＝Codocの有料記事として同期済みの）公開記事を1件探す。"""
    s = requests.Session()
    s.auth = AUTH
    page = 1
    while True:
        r = s.get(
            f"{SITE_URL}/wp-json/wp/v2/posts",
            params={"per_page": 50, "page": page, "status": "publish", "context": "edit",
                    "_fields": "id,slug,link,title"},
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for p in batch:
            if not NOTE_SLUG_PATTERN.match(p["slug"]):
                continue
            meta_r = s.get(f"{SITE_URL}/wp-json/wp/v2/posts/{p['id']}",
                            params={"context": "edit", "_fields": "id,slug,link,title,meta"}, timeout=30)
            meta_r.raise_for_status()
            full = meta_r.json()
            entry_code = full.get("meta", {}).get("codoc_entry_code")
            if entry_code:
                return {
                    "id": full["id"],
                    "slug": full["slug"],
                    "link": full["link"],
                    "title": full["title"]["rendered"],
                    "entry_code": entry_code,
                    "meta": full["meta"],
                }
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    raise RuntimeError("codoc_entry_code を持つ公開記事が見つかりませんでした")


def set_meta(session: requests.Session, post_id: int, meta: dict) -> None:
    r = session.post(f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}", json={"meta": meta}, timeout=30)
    r.raise_for_status()


def clean_search_query(raw_title: str) -> str:
    """タイトルからサイト内検索に使う短い平文クエリを作る。
    note記事のタイトルはHTMLエンティティ（&#8230;等）と絵文字・記号を大量に
    含むため、素朴に空白区切りで単語分割すると絵文字やエンティティの残骸
    （'8230'等）が紛れ込み、検索が0件になることを確認した。日本語には
    英語のような単語間スペースが無いため、「英数字・ひらがな・カタカナ・
    漢字の連続部分」だけを正規表現で拾い、十分な長さ（6文字以上）を持つ
    最初の1つを検索クエリとして使う。
    """
    text = html.unescape(raw_title)
    runs = re.findall(r"[0-9A-Za-zぁ-んァ-ヶ一-龠]+", text)
    for run in runs:
        if len(run) >= 6:
            return run[:14]
    return ("".join(runs) or text)[:14]


def parse_count(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0


def main() -> None:
    from playwright.sync_api import sync_playwright

    s = requests.Session()
    s.auth = AUTH

    print("=" * 60)
    print("ステップ0: 有料記事（codoc_entry_code保持）を1件特定")
    print("=" * 60)
    target = find_paid_note_post()
    post_id = target["id"]
    original_meta = {
        "codoc_cached_price": target["meta"].get("codoc_cached_price"),
        "codoc_cached_purchased_count": target["meta"].get("codoc_cached_purchased_count"),
        "codoc_cache_updated_at": target["meta"].get("codoc_cache_updated_at"),
    }
    print(f"対象記事: id={post_id} slug={target['slug']} title={target['title']!r}")
    print(f"元のmeta: {original_meta}")

    reverted = False
    try:
        print("\n" + "=" * 60)
        print("ステップ1: 購入数postmetaを一時的に1へ書き換え")
        print("=" * 60)
        # cache_updated_at を「今」にすることで、詳細ページ表示時のライブ再取得
        # （maybe_live_refresh_codoc_cache_for_post、直近300秒はスキップ）が
        # 実際のCodoc APIへアクセスしてこの注入値を上書きしないようにする。
        set_meta(s, post_id, {
            "codoc_cached_purchased_count": 1,
            "codoc_cache_updated_at": int(time.time()),
        })
        reverted = False
        print("[OK] codoc_cached_purchased_count=1, codoc_cache_updated_at=now に設定しました")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()

            print("\n" + "=" * 60)
            print("ステップ2: 詳細ページで『🛒 1人が購入』表示を確認")
            print("=" * 60)
            detail_url = target["link"] + ("&" if "?" in target["link"] else "?") + "_e2e=" + CACHE_BUST()
            page.goto(detail_url, wait_until="load", timeout=30000)

            purchased_el = page.locator(".nseb-stat-purchased").first
            page.wait_for_function(
                "el => el && !el.classList.contains('nseb-skeleton')",
                arg=purchased_el.element_handle(),
                timeout=20000,
            )
            purchased_text = purchased_el.inner_text().strip()
            print(f"詳細ページの購入バッジ: {purchased_text!r}")
            assert "1" in purchased_text and "購入" in purchased_text, (
                f"[FAIL] 詳細ページに『🛒 1人が購入』が表示されていません（実際: {purchased_text!r}）"
            )
            print("[OK] 詳細ページに『🛒 1人が購入』が表示されている")

            print("\n" + "=" * 60)
            print("ステップ3: サイト内検索結果（一覧カード）で『🛒1』バッジを確認")
            print("=" * 60)
            # タイトルの一部で検索し、一覧カードのテンプレートで対象記事が
            # 描画されるページを開く（トップページのページネーションを
            # 手繰るより確実に対象記事を1ページ目に出せるため）。
            query = clean_search_query(target["title"])
            search_url = f"{SITE_URL}/?s={requests.utils.quote(query)}&_e2e={CACHE_BUST()}"
            page.goto(search_url, wait_until="load", timeout=30000)

            card_selector = f"article#post-{post_id}"
            card = page.locator(card_selector).first
            try:
                card.wait_for(state="visible", timeout=15000)
            except Exception:
                raise AssertionError(
                    f"[FAIL] 検索結果ページ（query={query!r}）に対象記事(post-{post_id})のカードが"
                    "見つかりませんでした"
                )

            purchased_badge_selector = f"{card_selector} .nseb-card-purchased"
            page.wait_for_selector(purchased_badge_selector, timeout=15000)
            badge_text = page.locator(purchased_badge_selector).first.inner_text().strip()
            print(f"一覧カードの購入バッジ: {badge_text!r}")
            assert parse_count(badge_text) == 1, (
                f"[FAIL] 一覧カードの購入バッジが『1』になっていません（実際: {badge_text!r}）"
            )
            print("[OK] 一覧カードに『🛒1』バッジが表示されている")

            browser.close()

        print("\n" + "=" * 60)
        print(f"検証対象: post_id={post_id} title={target['title']!r}")
        print("E2E TEST PASSED")
        print("=" * 60)
    finally:
        print("\n[後処理] postmetaを元の値へ復元します...")
        try:
            set_meta(s, post_id, original_meta)
            reverted = True
            print(f"[OK] post_id={post_id} のmetaを復元しました: {original_meta}")
        except requests.RequestException as e:
            print(f"[警告] post_id={post_id} のmeta復元に失敗しました（手動確認が必要）: {e}")
        if not reverted:
            print("[警告] 復元処理が完了していません。手動での確認を推奨します。")


if __name__ == "__main__":
    main()
