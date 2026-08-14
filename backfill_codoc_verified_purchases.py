"""
Codoc「本当の」購入数バックフィル（2026-08-14）。

【背景】既存の backfill_codoc_cache.py / プラグインのライブ更新は、Codocの
公開API（GET /api/v1/storage/entries/{code}/body.json の purchased_count）を
データソースにしていた。しかし実機調査の結果、このpurchased_countは実際の
購入（Codocダッシュボードの「売上」ページに購入番号付きで記録され、
決済自体は完了している取引）を反映しないことがある（確認済みの実例：
記事コード e1BTEzpK9w は売上ページに購入記録が存在するのに公開APIの
purchased_countはずっと0のままだった）。

一方、Codocダッシュボードの記事一覧ページ（https://codoc.jp/me/entries、
要ログイン）の各記事行にある「shopping_basket（🛒）」アイコンの数字は、
同じ記事で正しく実購入数（1）を表示していることを確認した。そのため
このスクリプトは、ログイン済みダッシュボード（chrome_user_data の
永続セッション）をPlaywrightで巡回し、この「shopping_basket」の数字を
真の購入数としてWordPress側へ同期する。

各記事行には対応するWordPress公開ページの完全URL（insert_linkアイコンの
href）が直接含まれているため、Codoc側の記事コードやWordPress側の
codoc_entry_codeメタを介した突き合わせを一切せず、URLのパス（＝スラッグ）
だけで直接マッチングできる。

【永続化・保護ロジック】既存の codoc_cached_purchased_count が既に
このスクリプトの取得値以上の場合は絶対に書き換えない（max()を取る）。
これにより、他の経路（ライブ更新等）で先に正しい値が入っていた場合に
このスクリプトが後退させることはない。

実行方法:
    python backfill_codoc_verified_purchases.py
"""

from __future__ import annotations

import json
import re
import sys
import time

import requests
from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

with open("wp_credentials.json", encoding="utf-8") as f:
    CREDS = json.load(f)
SITE_URL = CREDS["site_url"].rstrip("/")
SITE_HOST = SITE_URL.split("//", 1)[-1]

ROW_SPLIT_RE = re.compile(r'(?=<div class="entries-list-body">)')
URL_RE = re.compile(
    r'href="(https://[^"]*' + re.escape(SITE_HOST) + r'/[^"]*)" target="_blank">'
    r'<i title="[^"]*" class="material-icons">insert_link'
)
SHOP_RE = re.compile(r"shopping_basket</i>\s*(?:<a[^>]*>)?\s*([0-9]+)\s*(?:</a>)?")


def parse_entries_page(html: str) -> list[tuple[str, int]]:
    blocks = ROW_SPLIT_RE.split(html)
    results = []
    for b in blocks[1:]:
        m_url = URL_RE.search(b)
        m_shop = SHOP_RE.search(b)
        if m_url and m_shop:
            results.append((m_url.group(1), int(m_shop.group(1))))
    return results


def extract_slug(url: str) -> str | None:
    path = url.split(SITE_HOST, 1)[-1].strip("/")
    if not path:
        return None
    return path.split("/")[0].split("?")[0]


def scrape_all_entries() -> list[tuple[str, int]]:
    print("Codocダッシュボード（/me/entries）から全記事の購入数を取得中...")
    all_rows: list[tuple[str, int]] = []
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            "chrome_user_data", channel="chrome", headless=True, viewport=None,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page_num = 1
        while True:
            url = f"https://codoc.jp/me/entries?page={page_num}"
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(1200)
            html = page.content()
            rows = parse_entries_page(html)
            if not rows:
                break
            all_rows.extend(rows)
            if page_num % 5 == 0 or page_num == 1:
                print(f"  page {page_num}: 累計 {len(all_rows)}件")
            page_num += 1
            if page_num > 200:  # 安全弁（無限ループ防止）
                print("  [警告] ページ数が200を超えたため打ち切りました")
                break
        context.close()
    print(f"取得完了: 全{len(all_rows)}件\n")
    return all_rows


def build_slug_to_id_map(session: requests.Session) -> dict[str, int]:
    print("WordPress公開記事の一覧を取得中...")
    slug_to_id: dict[str, int] = {}
    wp_page = 1
    while True:
        r = session.get(
            f"{SITE_URL}/wp-json/wp/v2/posts",
            params={"per_page": 100, "page": wp_page, "status": "publish", "_fields": "id,slug"},
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for p in batch:
            slug_to_id[p["slug"]] = p["id"]
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if wp_page >= total_pages:
            break
        wp_page += 1
    print(f"WordPress公開記事: {len(slug_to_id)}件\n")
    return slug_to_id


def main() -> None:
    all_rows = scrape_all_entries()

    s = requests.Session()
    s.auth = (CREDS["username"], CREDS["application_password"])
    slug_to_id = build_slug_to_id_map(s)

    updated = 0
    skipped_no_change = 0
    skipped_no_match = 0
    failed: list[tuple[int | str, str]] = []

    for i, (url, scraped_count) in enumerate(all_rows, start=1):
        slug = extract_slug(url)
        post_id = slug_to_id.get(slug) if slug else None
        if not post_id:
            skipped_no_match += 1
            continue
        try:
            r = s.get(
                f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}",
                params={"context": "edit", "_fields": "meta"},
                timeout=30,
            )
            r.raise_for_status()
            current = int(r.json().get("meta", {}).get("codoc_cached_purchased_count") or 0)
            # 保護ロジック：スクレイピングした値が現在のキャッシュ値以下なら何もしない
            # （後退させない。max()を取る）。
            if scraped_count <= current:
                skipped_no_change += 1
                continue
            upd = s.post(
                f"{SITE_URL}/wp-json/wp/v2/posts/{post_id}",
                json={"meta": {
                    "codoc_cached_purchased_count": scraped_count,
                    "codoc_cache_updated_at": int(time.time()),
                }},
                timeout=30,
            )
            if upd.status_code in (200, 201):
                updated += 1
            else:
                failed.append((post_id, f"wp_update_failed:{upd.status_code}"))
        except Exception as e:
            failed.append((post_id, str(e)))

        if i % 50 == 0 or i == len(all_rows):
            print(f"{i}/{len(all_rows)}件処理")
        time.sleep(0.05)

    print()
    print(
        f"完了: 更新{updated}件 / 既に同値以上のため変更不要{skipped_no_change}件 / "
        f"対応WordPress記事なし{skipped_no_match}件 / 失敗{len(failed)}件"
    )
    if failed:
        print("失敗内訳(先頭20件):", failed[:20])


if __name__ == "__main__":
    main()
