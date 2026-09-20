"""
note過去記事の差分自動検知・更新スクリプト。

【重要な制約（実装前に実機調査で確認した事実）】
note.com公開API（一覧API `/api/v2/creators/{user}/contents` および
個別記事詳細API `/api/v3/notes/{key}`）、および記事ページ自体のHTML
（OGP等のmetaタグ）のいずれにも、「記事の更新日時」（updated_at /
modified_at 等）を示すフィールドは一切存在しないことを2026-09-20に
実機調査で確認した（あるのは publish_at / created_at という「公開日時」
のみで、加筆修正のたびに更新される値ではない）。
そのため、「メタデータだけを見て、実際に変更があった記事だけを軽量に
絞り込む」という差分検知は技術的に不可能。唯一確実な方法は、既存の
auto_sync_blogs.sync_note_updates() と同じく、本文HTMLを実際に取得して
前回保存したハッシュ値（auto_sync_state.json）と比較する方式のみ。
本スクリプトはこの制約を前提に、sync_note_updates() を対象範囲（日数
ウィンドウ）だけ変えてラップする形で実装している
（sync_monthly_maintenance.py と同じ設計方針）。

【対象範囲】
- 引数なし（デフォルト）: 公開から90日以内の記事のみを対象に本文を
  再取得し、ハッシュ比較する。90日以内の記事数は通常数十件程度のため、
  1回のプロセス実行で問題なく完走できる。
- --all: 全note記事（約360件）が対象。360件規模の連続Playwright操作は
  メモリ逼迫でバックグラウンドプロセスが強制終了するリスクがあることを
  backfill_video_timestamp_links.py の運用で実機確認済みのため、
  --batch-size による分割実行に対応する。1つのバッチを処理し終えたら
  プロセスを正常終了し（OSレベルでメモリを完全解放するため）、シェル側
  で「未確認の記事が無くなるまで本プロセスを繰り返し起動する」運用を
  想定する。終了コード: 0=今回の全件確認サイクルが完了、
  2=まだ未確認の記事が残っている。
  一時的な進捗ファイル（sync_updated_notes_progress.json）で「今回の
  全件確認サイクルで、本文を実際に確認済みの記事キー」を記録し、
  サイクルが完全に完了した時点で自動的に削除する。
  【重要】これは backfill_*.py の進捗ファイルのような恒久的なスキップ
  記録ではない。本文が変わっていない記事も「確認はした」という記録が
  一度きり残るだけで、次に --all を実行するときは必ずゼロから
  やり直す（そうしないと2回目以降の定期実行で何も検知しなくなる）。

実行方法:
    python sync_updated_notes.py                                    # ドライラン（直近90日）
    python sync_updated_notes.py --execute                           # 本番実行（直近90日）
    python sync_updated_notes.py --execute --limit 5                 # 件数を絞った試験実行
    python sync_updated_notes.py --execute --all --batch-size 30     # 全件を30件ずつ分割実行
    python sync_updated_notes.py --execute --all                     # 全件を1回で実行（試験用途以外は非推奨）

デスクトップ等から実行する場合は「note差分同期（直近90日）.bat」を使用する。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core

SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_PATH = SCRIPT_DIR / "sync_updated_notes_progress.json"

DEFAULT_WINDOW_DAYS = 90


def load_checked_keys() -> set[str]:
    if PROGRESS_PATH.exists():
        return set(json.loads(PROGRESS_PATH.read_text(encoding="utf-8")))
    return set()


def save_checked_keys(keys: set[str]) -> None:
    PROGRESS_PATH.write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8")


def report_result(log_rows: list[dict], execute: bool) -> None:
    core.append_log(log_rows)
    # sync_note_updates() は実際に更新した記事を result="success"、
    # ドライランで更新見込みと判定した記事を result="dry_run_would_update"
    # として log_rows に積む（変更なしの記事はそもそもログに積まれない）。
    updated = [r for r in log_rows if r["result"] in ("success", "dry_run_would_update")]

    print("\n" + "=" * 60)
    print("差分検知・更新の結果")
    print("=" * 60)
    if updated:
        print(f"更新{'された' if execute else '見込みの'}記事: {len(updated)}件")
        for r in updated:
            print(f"  - {r['title']}")
    else:
        print("更新対象の記事はありませんでした（差分なし）。")

    counts: dict[str, int] = {}
    for row in log_rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    print()
    for result, count in sorted(counts.items()):
        print(f"  {result}: {count}件")
    print(f"ログを保存しました: {core.LOG_PATH}")
    if not execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="note過去記事の差分自動検知・更新")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument("--all", action="store_true", help="直近90日ではなく全note記事（約360件）を対象にする")
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="--all指定時のみ有効。この回で本文を確認する最大件数。指定件数を確認したら"
             "プロセスを正常終了する（メモリを完全に解放するため、シェル側で本プロセスごと"
             "繰り返し起動する運用を想定）。終了コード: 0=今回のサイクルで全件確認完了、"
             "2=まだ未確認の記事が残っている。",
    )
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    state = core.load_state()

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    scope = "全note記事（約360件）" if args.all else f"直近{DEFAULT_WINDOW_DAYS}日以内の記事"
    print("=" * 60)
    print(f"note過去記事 差分自動検知・更新  実行モード: {mode}")
    print(f"対象範囲: {scope}")
    print("=" * 60)

    link_maps = core.fetch_link_maps(wp)
    print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

    articles = core.fetch_note_article_list()
    print(f"note公開済み記事一覧を取得しました: {len(articles)}件")

    if not args.all:
        # 通常モード：直近90日以内のみが対象のため、件数が少なく1回のプロセスで完走できる。
        log_rows = core.sync_note_updates(
            wp, articles, state, args.execute, args.limit, link_maps,
            window_days=DEFAULT_WINDOW_DAYS,
        )
        core.save_state(state)
        report_result(log_rows, args.execute)
        return

    # --all モード：全件対象。360件規模のためバッチ分割に対応する。
    checked = load_checked_keys()
    pending = [a for a in articles if a["key"] not in checked]
    print(f"今回のサイクルで未確認: {len(pending)}件（{len(checked)}件は確認済み）")

    remaining_after_this_batch = pending
    if args.batch_size:
        batch = pending[:args.batch_size]
        remaining_after_this_batch = pending[args.batch_size:]
    else:
        batch = pending
        remaining_after_this_batch = []

    if args.limit:
        batch = batch[:args.limit]

    if not batch:
        print("対象記事はありませんでした（このサイクルは全件確認済みです）。")
        if PROGRESS_PATH.exists():
            PROGRESS_PATH.unlink()
            print("全件確認サイクルが完了したため、進捗ファイルをリセットしました。")
        return

    # window_daysを実質無制限にし、force_keysは使わない
    # （force_keysを使うと sync_note_updates 側で「本文ハッシュが同じでも
    #   強制的に更新扱い」になり、差分がない記事まで書き込んでしまう
    #   ＝ユーザー要求の「差分がない記事はスキップ」を破ってしまうため）。
    log_rows = core.sync_note_updates(
        wp, batch, state, args.execute, None, link_maps,
        window_days=10 ** 6,
    )
    core.save_state(state)

    if args.execute:
        checked |= {a["key"] for a in batch}
        save_checked_keys(checked)

    report_result(log_rows, args.execute)

    if args.batch_size:
        if remaining_after_this_batch:
            print(f"[バッチ継続] 残り{len(remaining_after_this_batch)}件。プロセスを終了し、次のバッチのため再起動してください。")
            sys.exit(2)
        else:
            print("[バッチ完了] 今回の全件確認サイクルはこれで終わりです。")
            if args.execute and PROGRESS_PATH.exists():
                PROGRESS_PATH.unlink()
            sys.exit(0)


if __name__ == "__main__":
    main()
