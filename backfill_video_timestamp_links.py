"""
【全記事一括バックフィル】動画時間指定リンクの保護・重複防止を全note記事へ適用

背景: 2026-09-20、auto_sync_blogs.fetch_note_body_html() に
protect_video_timestamp_links() / ensure_visible_timestamp_link_before_embed()
を実装し、YouTube/ツイキャスの時間指定（?t=）付きリンクをクリッカブルな
テキストリンクとして保護しつつ、動画/カードの二重展開を防止できるように
した（単体記事 nb8e29ddba925 で実機検証済み、コミット a883115）。
本スクリプトはこれを既存のnote全記事（約360件）へ一括適用するための、
今回限りのバックフィル専用スクリプト。

【対象の判定】note.comへ実際に再アクセスし、fetch_note_body_html()経由で
本文を取得し直す必要がある（保護ロジックはこの関数の中にあるため、WP側の
raw content文字列を直接書き換えるbackfill_external_blogcards.py方式では
適用できない）。そのため、Playwrightでnote.comへ1件ずつアクセスする
auto_sync_blogs.sync_note_updates()相当の処理を、公開経過日数の
ウィンドウ制限なしに全件へ適用する（ロジックは同関数からの直接移植）。

【変更が無い記事は自動的にスキップされる】本文HTMLのハッシュ比較
（auto_sync_state.json の content_hash）により、今回の保護ロジックを
適用しても出力が変わらない記事（動画時間指定リンクを含まない大半の記事）
は「変更なし」として自動的にスキップされ、実際に差分が生じる記事のみ
WordPress側を更新する。

【夜間放置運用のための堅牢設計（CLAUDE.md「4. チェックポイント設計の
徹底」に準拠)】
- 1件ごとにtry/exceptで保護。リトライしても解消しない失敗はログに記録し、
  スクリプト全体は止めずに次の記事へ進む。
- 1件処理するたびに進捗ファイル
  （backfill_video_timestamp_links_progress.json）へ結果を保存する。
  既に処理済み（success/no_change）の記事は再実行時に自動的にスキップ
  され、中断からの再開時に手戻りが無い（--recheck-allで無視して全件
  再確認も可能）。
- 本文を実際に更新した直後、auto_sync_state.json（content_hash）も
  都度保存するため、大量の記事を処理中に中断しても、次回起動時に
  「本当に処理が終わっていない記事」だけが再処理対象になる。
- WordPress REST APIへの通信はauto_sync_blogs.WP._requestの自動リトライ
  （指数バックオフ、最大3回）をそのまま利用する。

実行方法:
    python backfill_video_timestamp_links.py                     # ドライラン（まず必ずこれで確認）
    python backfill_video_timestamp_links.py --execute            # 本番実行
    python backfill_video_timestamp_links.py --execute --limit 5  # 件数を絞った試験実行
    python backfill_video_timestamp_links.py --execute --recheck-all  # 進捗キャッシュを無視し全件再確認
"""

from __future__ import annotations

import argparse
import csv
import gc
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core

SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_PATH = SCRIPT_DIR / "backfill_video_timestamp_links_progress.json"
LOG_PATH = SCRIPT_DIR / "backfill_video_timestamp_links_log.csv"

DONE_STATUSES = ("success", "no_change", "not_found_in_wp")  # 再実行時にスキップしてよい確定ステータス


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        import json
        return __import__("json").loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {}


def save_progress_entry(progress: dict, key: str, result: dict) -> None:
    import json
    progress[key] = result
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "task", "post_id", "slug", "title", "result", "detail"])
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="note全記事への動画時間指定リンク保護バックフィル")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    parser.add_argument(
        "--recheck-all", action="store_true",
        help="進捗ファイルのキャッシュを無視し、note全記事を本文取得し直して再確認する",
    )
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="この回で処理する最大件数。指定件数を処理したらプロセスを正常終了する"
             "（メモリを完全に解放するため、シェル側で本プロセスごと繰り返し起動する運用を想定）。"
             "終了コード: 0=全件処理完了（もう未処理なし）、2=まだ未処理が残っている、1=致命的エラー。",
    )
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    state = core.load_state()
    note_state = state.setdefault("note", {})

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print("=" * 60)
    print(f"動画時間指定リンク保護 全記事バックフィル  実行モード: {mode}")
    print("=" * 60)

    link_maps = core.fetch_link_maps(wp)
    print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

    articles = core.fetch_note_article_list()
    print(f"note公開済み記事一覧を取得しました: {len(articles)}件")

    progress = load_progress()
    if args.recheck_all:
        pending_all = articles
        print("--recheck-all: 進捗ファイルのキャッシュを無視し、全件を再確認します")
    else:
        pending_all = [a for a in articles if a["key"] not in progress or progress[a["key"]].get("status") not in DONE_STATUSES]
        print(f"未処理・要再確認: {len(pending_all)}件（進捗ファイルから{len(articles) - len(pending_all)}件をスキップ）")

    # 【2026-09-20追記：30件ごとの分割実行】メモリ逼迫でバックグラウンドタスクが
    # 2度強制終了された対策として、--batch-sizeを指定した場合はこの回で
    # その件数までしか処理せず、プロセスをそこで正常終了させる（OSレベルで
    # メモリを完全解放するため）。シェル側で「未処理が無くなるまでこの
    # プロセスを繰り返し起動する」ループと組み合わせて使う。
    remaining_after_this_batch = pending_all
    if args.batch_size:
        remaining_after_this_batch = pending_all[args.batch_size:]
        pending = pending_all[:args.batch_size]
    else:
        pending = pending_all

    if args.limit:
        pending = pending[:args.limit]

    total_articles = len(articles)

    if not pending:
        print("対象記事はありませんでした（全件処理済み）。")
        return

    log_rows: list[dict] = []
    counts: dict[str, int] = {}
    processed_in_this_run = 0

    from playwright.sync_api import sync_playwright
    from backfill_codoc_subscription_linkage import process_entry as relink_codoc_subscription

    with sync_playwright() as pw:
        context, page = core.get_note_browser_page(pw, headless=True)
        try:
            for i, a in enumerate(pending, start=1):
                key = a["key"]
                slug = key
                title = a.get("title") or ""
                status = "error"
                post_id_for_log = None
                # 【2026-09-20追記：メモリ逼迫によるバックグラウンドタスク強制終了対策】
                # 358件規模の連続Playwright操作（note.comへの毎回のフルページ遷移＋
                # 大きな本文HTML文字列の生成・変換）で、システム全体のメモリ逼迫により
                # 実行中のバックグラウンドタスクが強制終了される事故が発生した
                # （135件処理時点で停止）。ループの各イテレーションで使う大きめの
                # ローカル変数をあらかじめNoneで初期化しておき、finally節で確実に
                # Noneへ再代入してから明示的にgc.collect()を呼ぶことで、次の記事の
                # 処理に入る前にメモリのピークを都度解放する。
                post = None
                body_html = None
                content = None
                existing_content = None
                update_payload = None
                try:
                    post = wp.find_post_by_slug(slug)
                    if post is None:
                        # まだWordPress側に存在しない記事（新規投稿の対象。本バックフィルの対象外）。
                        # 【2026-09-20追記：ログ改善】以前はここでcontinueするだけでCSVログに
                        # 一切記録されず、原因調査のたびにprogress.jsonのキーをnote.com公開API
                        # へ逆引きする必要があった。スキップ理由（公開日時込み）をCSVにも
                        # 残すことで、オーナーがcsvを開くだけで即座に判別できるようにする。
                        status = "not_found_in_wp"
                        log_rows.append(core.log_row(
                            "backfill_video_ts", None, slug, title, status,
                            f"WordPress未同期の新着記事（note公開日時: {a.get('publish_at') or '不明'}）",
                        ))
                        continue
                    post_id_for_log = post["id"]

                    print(f"[{i}/{len(pending)}] {title[:50]}")

                    if not args.execute:
                        # ドライランでは実際にnote.comへアクセスして差分の有無だけ確認する
                        body_html = core.fetch_note_body_html(page, a["url"])
                        change_signature = (
                            (title or "") + "\n"
                            + ",".join(sorted(a.get("tags") or [])) + "\n"
                            + body_html
                        )
                        new_hash = core.body_hash(change_signature)
                        old_hash = note_state.get(key, {}).get("content_hash")
                        if old_hash == new_hash:
                            print("    変更なし")
                            status = "no_change"
                        else:
                            print("    [検出] 保護ロジック適用により差分あり（更新見込み）")
                            status = "dry_run_would_update"
                        log_rows.append(core.log_row("backfill_video_ts", post_id_for_log, slug, title, status))
                        continue

                    body_html = core.fetch_note_body_html(page, a["url"])
                    change_signature = (
                        (title or "") + "\n"
                        + ",".join(sorted(a.get("tags") or [])) + "\n"
                        + body_html
                    )
                    new_hash = core.body_hash(change_signature)
                    old_hash = note_state.get(key, {}).get("content_hash")

                    if old_hash == new_hash:
                        print("    変更なし")
                        status = "no_change"
                        log_rows.append(core.log_row("backfill_video_ts", post_id_for_log, slug, title, status))
                        continue

                    print("    [変更を検出]")

                    # 既存記事のCodoc価格は変更せず維持したまま本文だけ差し替える
                    existing_content = post.get("content", {}).get("raw", "") or ""
                    existing_price = core.extract_codoc_price(existing_content)

                    content = body_html
                    if existing_price and existing_price > 0:
                        with_marker = core.insert_codoc_paywall_at_element(content, a.get("paywall_element_id"), existing_price)
                        content = with_marker if with_marker is not None else core.insert_codoc_paywall_simple(content, existing_price)

                    # 本文差し替え時に案内枠（サブスク登録ボタン込み）が消えないよう、
                    # 新規投稿時と同じ形式で必ず付け直す。
                    content = core.build_subscription_upsell_box(a["url"]) + content
                    content = core.insert_full_title_box(content, title)
                    note_map, exblog_map = link_maps
                    content, _ = core.convert_internal_links(content, note_map, exblog_map)
                    content = core.finalize_external_blogcard_urls(content)
                    error_markers = core.find_error_text_markers(content)
                    if error_markers:
                        raise RuntimeError(f"本文にサーバーエラー文字列が混入: {error_markers}")

                    full_title = title or post.get("title", {}).get("raw", "")

                    update_payload = {
                        "content": content,
                        "excerpt": full_title,
                        "meta": {
                            "note_full_title": full_title,
                            "codoc_cached_price": existing_price or 0,
                            "codoc_cache_updated_at": int(time.time()),
                            "nseb_like_count": int(a.get("like_count") or 0),
                        },
                    }
                    existing_categories = post.get("categories") or []
                    if core.NOTE_CATEGORY_ID not in existing_categories:
                        update_payload["categories"] = sorted(set(existing_categories) | {core.NOTE_CATEGORY_ID} - {1})
                        print(f"    [フォールバック] post_id={post['id']} のnoteカテゴリーを自己修復します")

                    tag_ids = [wp.get_or_create_tag(t) for t in (a.get("tags") or [])]
                    tag_ids = [t for t in tag_ids if t]
                    update_payload["tags"] = tag_ids

                    wp.update_post(post["id"], update_payload)
                    note_state[key] = {"content_hash": new_hash, "price": existing_price}
                    core.save_state(state)  # チェックポイント: 都度state.jsonへ保存
                    print(f"    [OK] post_id={post['id']} を更新しました")
                    status = "success"
                    log_rows.append(core.log_row("backfill_video_ts", post["id"], slug, title, status))

                    entry_code = (post.get("meta") or {}).get("codoc_entry_code")
                    if not entry_code:
                        fresh = wp.get_post(post["id"])
                        entry_code = (fresh.get("meta") or {}).get("codoc_entry_code")
                    if entry_code:
                        try:
                            relink_result = relink_codoc_subscription(page, entry_code, True)
                            print(f"    [Codoc再紐付け] entry_code={entry_code} -> {relink_result['status']}")
                            log_rows.append(
                                core.log_row("backfill_video_ts", post["id"], slug, title, f"codoc_relink_{relink_result['status']}")
                            )
                        except Exception as e:
                            print(f"    [警告] Codoc再紐付けに失敗: {e}")
                            log_rows.append(core.log_row("backfill_video_ts", post["id"], slug, title, "codoc_relink_error", str(e)[:300]))

                except Exception as e:
                    status = "error"
                    print(f"    [失敗] {e}")
                    log_rows.append(core.log_row("backfill_video_ts", post_id_for_log, slug, title, "failed", str(e)[:300]))
                finally:
                    save_progress_entry(progress, key, {"status": status})
                    counts[status] = counts.get(status, 0) + 1
                    processed_in_this_run += 1
                    # メモリ解放: 大きなローカル変数の参照を切ってから即座にGCを回す
                    post = None
                    body_html = None
                    content = None
                    existing_content = None
                    update_payload = None
                    gc.collect()

                    # 【2026-09-20追記：だんまり防止の定期進捗ログ】5件処理するごとに
                    # 全体（363件）に対する進捗をコンソールへ出力する。
                    if processed_in_this_run % 5 == 0 or processed_in_this_run == len(pending):
                        done_total = len(progress)  # 進捗ファイルの累計記録件数（全実行を通じた累計）
                        success_total = sum(1 for v in progress.values() if v.get("status") == "success")
                        skip_total = sum(1 for v in progress.values() if v.get("status") in ("no_change", "not_found_in_wp"))
                        remaining_total = max(total_articles - done_total, 0)
                        print(
                            f"    [進捗] 全{total_articles}件中 {done_total}件処理完了"
                            f"（成功: {success_total}件 / スキップ: {skip_total}件 / 残り: {remaining_total}件）"
                        )

                    if status != "not_found_in_wp":
                        time.sleep(core.REQUEST_DELAY_SECONDS)
                    time.sleep(0.2)  # システム負荷の急上昇を防ぐための小休止
        finally:
            context.close()

    append_log(log_rows)

    print("\n" + "=" * 60)
    print("完了サマリー")
    print("=" * 60)
    for s, n in sorted(counts.items()):
        print(f"  {s}: {n}件")
    print(f"ログを保存しました: {LOG_PATH}")
    print(f"進捗を保存しました: {PROGRESS_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)

    # 【2026-09-20追記：30件ごとの分割実行のための終了コード】--batch-size指定時、
    # シェル側の「未処理が0件になるまでこのプロセスを繰り返し起動するループ」に
    # 「まだ続きがあるか」を伝えるための終了コード。
    #   0 = このバッチで全件処理完了（もう未処理なし）
    #   2 = まだ未処理が残っている（シェル側は本プロセスを再度起動すること）
    if args.batch_size:
        if remaining_after_this_batch:
            print(f"[バッチ継続] 残り{len(remaining_after_this_batch)}件。プロセスを終了し、次のバッチのため再起動してください。")
            sys.exit(2)
        else:
            print("[バッチ完了] 未処理の記事はもうありません。")
            sys.exit(0)


if __name__ == "__main__":
    main()
