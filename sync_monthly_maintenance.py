"""
月次メンテナンススクリプト：

    1. 内容更新チェック（対象：公開から直近4ヶ月以内の記事のみ）
       note.com側の最新のタイトル・タグ・本文（HTML）と、前回同期時にローカルへ
       保存したハッシュ値（auto_sync_state.json）を比較し、差分がある記事だけ
       WordPressを更新する（auto_sync_blogs.sync_note_updates を再利用）。
       本文をREST APIで更新するとCodoc側の購読プラン紐付けチェックボックスが
       サイレントに解除される既知の副作用があるため、更新した記事は
       このスクリプトの最後に自動で購読プラン紐付けを再確認・再設定する。

    2. 価格更新チェック（90日ルール）
       公開から90日（約3ヶ月）以上経過し、まだCodoc価格が既定の値下げ後価格
       （100円）より高い記事を自動的に100円へ値下げする
       （auto_sync_blogs.sync_codoc_discount を再利用。300円→100円に限らず、
       100円より高い価格であれば同様に対象になる＝既存仕様のまま）。

対象を「直近4ヶ月」に限定しているのは、全件（300件超）を毎回Playwrightで
巡回するとサーバー負荷・実行時間・タイムアウトによるクラッシュのリスクが
大きいため。4ヶ月より古い記事の本文修正が必要になった場合は、
auto_sync_blogs.py --only update-note --key <note記事キー> で個別に
強制更新すること（このスクリプトの対象外）。

実行方法:
    python sync_monthly_maintenance.py                    # ドライラン（まず必ずこれで確認）
    python sync_monthly_maintenance.py --execute           # 本番実行
    python sync_monthly_maintenance.py --execute --limit 5 # 件数を絞った試験実行
    python sync_monthly_maintenance.py --window-days 150   # 更新チェック対象期間を変更（既定120日≒4ヶ月）

デスクトップ等から実行する場合は 月次メンテ.bat を使用する。
"""

from __future__ import annotations

import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core

MONTHLY_UPDATE_WINDOW_DAYS = 120  # 約4ヶ月。全件走査によるクラッシュ・負荷を避けるための上限


def main() -> None:
    parser = argparse.ArgumentParser(description="月次メンテナンス: 直近4ヶ月の更新追従 + 90日値下げ")
    parser.add_argument("--execute", action="store_true", help="実際に変更を行う（指定しない場合はドライラン）")
    parser.add_argument("--limit", type=int, default=None, help="各処理の対象件数の上限（試験実行用）")
    parser.add_argument(
        "--window-days", type=int, default=MONTHLY_UPDATE_WINDOW_DAYS,
        help=f"内容更新チェックの対象期間（公開からの日数、既定{MONTHLY_UPDATE_WINDOW_DAYS}日≒4ヶ月）",
    )
    parser.add_argument(
        "--only", choices=["update-note", "discount-codoc"], default=None,
        help="指定した処理だけを実行する（省略時は両方実行）",
    )
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    state = core.load_state()

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"
    print("=" * 60)
    print(f"月次メンテナンススクリプト開始  実行モード: {mode}")
    print(f"内容更新チェックの対象期間: 公開から{args.window_days}日以内")
    print(f"値下げチェックの対象条件: 公開から{core.CODOC_DISCOUNT_AFTER_DAYS}日以上経過 かつ 価格>{core.CODOC_DISCOUNT_PRICE}円")
    if args.only:
        print(f"実行対象: {args.only} のみ")
    print("=" * 60)

    all_log_rows: list[dict] = []

    if args.only in (None, "update-note"):
        link_maps = core.fetch_link_maps(wp)
        print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

        note_articles = core.fetch_note_article_list()
        print(f"note公開済み記事一覧を取得しました: {len(note_articles)}件")

        all_log_rows += core.sync_note_updates(
            wp, note_articles, state, args.execute, args.limit, link_maps,
            window_days=args.window_days,
        )
        core.save_state(state)

    if args.only in (None, "discount-codoc"):
        all_log_rows += core.sync_codoc_discount(wp, args.execute, args.limit)

    print("\n" + "=" * 60)
    print("Codoc購読プラン紐付けの横断チェック")
    core.relink_touched_posts(wp, all_log_rows, args.execute)

    core.append_log(all_log_rows)

    print("\n" + "=" * 60)
    counts: dict[str, int] = {}
    for row in all_log_rows:
        counts[row["result"]] = counts.get(row["result"], 0) + 1
    if counts:
        for result, count in counts.items():
            print(f"  {result}: {count}件")
    else:
        print("  対象記事はありませんでした。")
    print(f"ログを保存しました: {core.LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
