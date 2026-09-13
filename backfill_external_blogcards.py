"""
note記事の「外部リンク埋め込み」一括ブログカード化バックフィルスクリプト。

【背景】2026-09-13、note.comの外部リンク埋め込みウィジェット
（<figure embedded-service="external-article">...</figure>）を検出して
Cocoonのブログカードショートコード（[blogcard url="..."]）へ変換する処理
（auto_sync_blogs.convert_external_article_embeds_to_blogcards）を実装し、
新規投稿・更新追従の経路（sync_new_note_posts / sync_note_updates）に
組み込んだ。しかしこれは「今後」の同期にしか効かないため、過去に既に
投稿済みのnote記事（約358件）の本文には、この埋め込みウィジェットが
未変換のまま残っている（Cocoon側の対応CSSが無いため、サムネイル無しの
青文字テキストリンクの羅列に見える）。本スクリプトはそれを一括で
検出・変換するバックフィル専用スクリプト。

【対象の判定】note.comへ再アクセスする必要はない。WordPress側に保存済みの
本文（wp.get_post()のcontent.raw）に 'embedded-service="external-article"'
という文字列マーカーが含まれる記事のみが変換対象。既に変換済みの記事
（新規投稿・更新追従の経路を通った直近の記事等）はこのマーカーを持たない
ため自動的にスキップされる。

【Codoc購読プラン紐付けの保護（最重要）】本文をREST APIで更新すると
Codoc側の購読プラン紐付けチェックボックスがサイレントに解除される既知の
副作用があるため（backfill_codoc_subscription_linkage.py のdocstring参照）、
本文を実際に更新した記事に限り、更新直後にCodocの購読プラン紐付けを
再実行し、process_entry() 内部の再読み込み検証（保存後にページを再取得し
チェックボックスの状態を確認する）まで確実に行う。本文を更新しなかった
記事（変換対象が無かった記事）はCodoc側には一切触れない。

【堅牢設計（夜間放置運用を想定）】
- WordPress REST APIへの通信は auto_sync_blogs.WP._request が最大3回まで
  自動リトライする（指数バックオフ、タイムアウト60/90秒。2026-09-13の
  ReadTimeout対応と共通の基盤をそのまま利用）。
- 1件ごとにtry/exceptで囲み、（リトライしても解消しない）失敗が起きても
  ログに記録して次の記事へ進む。スクリプト全体は止めない。
- 実際に本文を更新した記事の直後にのみ1〜2秒のランダムウェイトを入れ、
  サーバー負荷を抑える（本文に変換対象が無かった記事は待たずに次へ進む）。
- 1件処理するたびに進捗ファイル（backfill_external_blogcards_progress.json）
  へ結果を保存する。既に "success"（変換・紐付け確認済み）または
  "no_change"（変換対象なし）と記録された記事は、再実行時に自動的に
  スキップされ、未処理分・エラーだった記事だけが再試行される
  （CLAUDE.md「4. チェックポイント設計の徹底」に準拠。夜間に処理が
  中断しても、次回実行時に手戻りなく再開できる）。

実行方法:
    python backfill_external_blogcards.py                     # ドライラン（まず必ずこれで確認）
    python backfill_external_blogcards.py --execute            # 本番実行
    python backfill_external_blogcards.py --execute --limit 2  # 件数を絞った試験実行
    python backfill_external_blogcards.py --execute --recheck-all  # 進捗キャッシュを無視し全件再確認

デスクトップ等から実行する場合は 全件カード化メンテ.bat を使用する。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core
from backfill_codoc_subscription_linkage import process_entry as relink_codoc_subscription

SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_PATH = SCRIPT_DIR / "backfill_external_blogcards_progress.json"
LOG_PATH = SCRIPT_DIR / "backfill_external_blogcards_log.csv"

EXTERNAL_ARTICLE_MARKER = 'embedded-service="external-article"'
DONE_STATUSES = ("success", "no_change")  # 再実行時にスキップしてよい確定ステータス


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {}


def save_progress_entry(progress: dict, post_id: int, result: dict) -> None:
    progress[str(post_id)] = result
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "post_id", "slug", "title", "status", "detail"])
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def log_row(post_id: int, slug: str, title: str, status: str, detail: str = "") -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "post_id": post_id, "slug": slug, "title": title,
        "status": status, "detail": detail,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="note記事の外部リンク埋め込み一括ブログカード化バックフィル")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    parser.add_argument(
        "--recheck-all", action="store_true",
        help="進捗ファイルのキャッシュを無視し、noteカテゴリー全件を本文から再確認する",
    )
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print("=" * 60)
    print(f"note記事 外部リンクのブログカード化バックフィル  実行モード: {mode}")
    print("=" * 60)

    posts = core.fetch_all_wp_posts_with_dates(wp, categories=core.NOTE_CATEGORY_ID)
    print(f"noteカテゴリー記事: {len(posts)}件")

    progress = load_progress()
    if args.recheck_all:
        pending_posts = posts
        print("--recheck-all: 進捗ファイルのキャッシュを無視し、全件の本文を再確認します")
    else:
        pending_posts = [
            p for p in posts
            if str(p["id"]) not in progress or progress[str(p["id"])].get("status") not in DONE_STATUSES
        ]
        print(f"未処理・要再確認: {len(pending_posts)}件（進捗ファイルから{len(posts) - len(pending_posts)}件をスキップ）")

    if not pending_posts:
        print("対象記事はありませんでした（全件処理済み）。")
        return

    content_by_id = core.fetch_posts_content_by_ids(wp, [p["id"] for p in pending_posts])

    targets = []
    for p in pending_posts:
        content = content_by_id.get(p["id"], "")
        if EXTERNAL_ARTICLE_MARKER in content:
            targets.append(p)
        else:
            # 変換対象なし（本文に未変換の埋め込みが無い＝既に変換済み or 元々外部リンクが無い）。
            # 確定ステータスとして進捗に記録し、次回以降は本文取得ごとスキップする。
            save_progress_entry(progress, p["id"], {"status": "no_change"})

    print(f"未変換の外部リンク埋め込みを含む記事: {len(targets)}件")

    if args.limit:
        targets = targets[:args.limit]

    log_rows: list[dict] = []
    counts: dict[str, int] = {}

    pw_cm = None
    context = page = None
    if args.execute and targets:
        from playwright.sync_api import sync_playwright
        pw_cm = sync_playwright()
        pw = pw_cm.__enter__()
        context, page = core.get_note_browser_page(pw, headless=True)

    try:
        for i, p in enumerate(targets, start=1):
            post_id = p["id"]
            slug = p.get("slug", "")
            title = p["title"]["raw"] if isinstance(p.get("title"), dict) else str(p.get("title", ""))
            print(f"[{i}/{len(targets)}] id={post_id} {title[:50]}")
            status = "error"
            wrote = False
            try:
                content = content_by_id.get(post_id, "")
                new_content = core.convert_external_article_embeds_to_blogcards(content)
                new_content = core.finalize_external_blogcard_urls(new_content)

                if new_content == content:
                    print("    変換対象なし（マーカー検出後の再確認で変化なし）")
                    status = "no_change"
                    log_rows.append(log_row(post_id, slug, title, status))
                    continue

                error_markers = core.find_error_text_markers(new_content)
                if error_markers:
                    raise RuntimeError(f"本文にサーバーエラー文字列が混入: {error_markers}")

                blogcard_count = new_content.count("[blogcard url=")
                if not args.execute:
                    print(f"    [検出] 変換すれば{blogcard_count}件のブログカードになる")
                    status = "dry_run_would_convert"
                    log_rows.append(log_row(post_id, slug, title, status, f"blogcard_count={blogcard_count}"))
                    continue

                wp.update_post(post_id, {"content": new_content})
                wrote = True
                print(f"    [OK] 本文を更新しました（blogcard {blogcard_count}件）")

                # Codoc購読プラン紐付けの再確認（本文を実際に更新した記事のみ）。
                fresh = wp.get_post(post_id)
                entry_code = (fresh.get("meta") or {}).get("codoc_entry_code")
                relink_status = "no_entry_code"
                if entry_code:
                    relink_result = relink_codoc_subscription(page, entry_code, True)
                    relink_status = relink_result["status"]
                    print(f"    [Codoc再紐付け] entry_code={entry_code} -> {relink_status}")

                status = "success"
                log_rows.append(log_row(post_id, slug, title, status, f"blogcard_count={blogcard_count};codoc_relink={relink_status}"))
            except Exception as e:
                status = "error"
                print(f"    [失敗] {e}")
                log_rows.append(log_row(post_id, slug, title, status, str(e)[:300]))
            finally:
                save_progress_entry(progress, post_id, {"status": status})
                counts[status] = counts.get(status, 0) + 1
                if wrote:
                    time.sleep(random.uniform(1.0, 2.0))
    finally:
        if context is not None:
            context.close()
        if pw_cm is not None:
            pw_cm.__exit__(None, None, None)

    append_log(log_rows)

    print("\n" + "=" * 60)
    for s, n in sorted(counts.items()):
        print(f"  {s}: {n}件")
    print(f"ログを保存しました: {LOG_PATH}")
    print(f"進捗を保存しました: {PROGRESS_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
