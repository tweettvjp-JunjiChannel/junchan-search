"""
週次運用スクリプト：note.comの新着記事のみをWordPressへ取り込み、
Codocの月額購読プラン（4133番、月額1,000円）へ自動紐付けする。

やること（それ以上は何もしない）:
    1. note.com側の公開記事一覧を取得し、WordPress側にまだ存在しない
       新着記事のみをダウンロード・新規投稿する（auto_sync_blogs.sync_new_note_posts）。
    2. 新規投稿した記事について、Codoc管理画面の「購読プラン販売」チェックボックスを
       自動でONにする（sync_new_note_posts内で新規投稿のたびに実行される他、
       本スクリプトの最後にも横断的な安全網としてもう一度確認する）。

既存記事の本文更新チェックや90日値下げチェックは対象外（月次メンテナンス
=sync_monthly_maintenance.py の担当）。週次はあくまで「新着の取りこぼしがないか」
を軽く回すためのスクリプトとして、処理範囲をあえて絞ってある。

実行方法:
    python sync_weekly.py                  # ドライラン（何も変更しない。まず必ずこれで確認）
    python sync_weekly.py --execute        # 本番実行
    python sync_weekly.py --execute --limit 3   # 件数を絞った試験実行

デスクトップ等から実行する場合は 週次実行.bat を使用する。
"""

from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core


def main() -> None:
    parser = argparse.ArgumentParser(description="週次運用: note新着記事の取り込み + Codoc購読プラン紐付け")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    state = core.load_state()

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print("=" * 60)
    print(f"週次運用スクリプト開始  実行モード: {mode}")
    print("=" * 60)

    link_maps = core.fetch_link_maps(wp)
    print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

    note_articles = core.fetch_note_article_list()
    print(f"note公開済み記事一覧を取得しました: {len(note_articles)}件")

    log_rows = core.sync_new_note_posts(wp, note_articles, state, args.execute, args.limit, link_maps)
    core.save_state(state)

    core.relink_touched_posts(wp, log_rows, args.execute)

    core.append_log(log_rows)

    print("\n" + "=" * 60)
    counts: dict[str, int] = {}
    for row in log_rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    if counts:
        for result, count in counts.items():
            print(f"  {result}: {count}件")
    else:
        print("  新着記事はありませんでした。")
    print(f"ログを保存しました: {core.LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
