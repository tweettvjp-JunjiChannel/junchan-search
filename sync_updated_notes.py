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

【2026-09-20追記：デフォルトを全件対象に変更】
「差分がない記事はスキップするので全件対象でも短時間で終わる」という
想定は誤り。差分の有無を判定するにはどのみち全記事についてnote.comへ
実際にアクセスし本文を取得する必要があり、この「判定コスト」自体は
スキップできない（スキップできるのは判定後の「書き込み」だけ）。
実機の全件バックフィル（backfill_video_timestamp_links.py）の実測値
から、全363件の確認には約3〜3.5時間かかると推定される。
この見積りをオーナーに報告した上で、「週次の日常バッチ（sync_weekly.py）
には組み込まず、全件差分チェックは独立した専用バッチ
（全件差分同期.bat）として、夜間放置等の任意タイミングで実行する」
という運用方針が確定した。デフォルト（引数なし）は全件を対象にし、
軽量に済ませたい場合のみ --recent-days で期間を絞れるようにする。

【対象範囲】
- 引数なし（デフォルト）: 全note記事（約360件）が対象。360件規模の
  連続Playwright操作はメモリ逼迫でプロセスが強制終了するリスクがある
  ことをbackfill_video_timestamp_links.pyの運用で実機確認済みのため、
  --batch-sizeによる分割実行に対応する。1つのバッチを処理し終えたら
  プロセスを正常終了し（OSレベルでメモリを完全解放するため）、バッチ
  ファイル側で「未確認の記事が無くなるまで本プロセスを繰り返し起動
  する」ループを組んで使う。終了コード: 0=今回の全件確認サイクルが
  完了、2=まだ未確認の記事が残っている。
  一時的な進捗ファイル（sync_updated_notes_progress.json）で「今回の
  全件確認サイクルで、本文を実際に確認済みの記事キー」を記録し、
  サイクルが完全に完了した時点で自動的に削除する。
  【重要】これはbackfill_*.pyの進捗ファイルのような恒久的なスキップ
  記録ではない。本文が変わっていない記事も「確認はした」という記録が
  一度きり残るだけで、次に全件モードを実行するときは必ずゼロから
  やり直す（そうしないと2回目以降の定期実行で何も検知しなくなる）。
- --recent-days N: 公開からN日以内の記事のみを対象にする軽量モード。
  対象件数が少ないため（90日以内で数十件程度）、1回のプロセス実行で
  問題なく完走できる。

実行方法:
    python sync_updated_notes.py --execute --batch-size 30   # 全件を30件ずつ分割実行（通常運用はこれ）
    python sync_updated_notes.py --recent-days 90              # 直近90日のみドライラン（軽量・試験向け）
    python sync_updated_notes.py --execute --recent-days 90    # 直近90日のみ本番実行
    python sync_updated_notes.py --execute --limit 5           # 件数を絞った試験実行

デスクトップ等から実行する場合は「全件差分同期.bat」を使用する
（夜間放置向け。バッチ分割ループを内蔵しているため、これ単体で
最後まで自動的に完走する）。

【2026-10-18追記：リストファイルによるピンポイント強制同期（--list-file）】
特定のnote記事だけを即座にWordPressへ反映したい場合のための運用モード。
テキストファイル（1行に1つnote記事のURL。空行・"#"始まりのコメント行は
無視。URLの代わりに記事キー "n..." を直接書いても可）を読み込み、
- 本文ハッシュによる差分判定・公開日ウィンドウを一切スキップし、
- WordPress側にスラッグ（=記事キー）で既に存在する記事は強制上書き更新
  （sync_note_updates の force_keys 経由）、
- 存在しない記事は新規投稿（sync_new_note_posts 経由）
する。新規・更新とも既存の通常経路の関数をそのまま呼ぶため、赤枠＋青枠、
Codoc価格維持・購読プラン紐付け、カテゴリー自己修復、タグ同期といった
既存の仕様はすべてそのまま適用される。
    python sync_updated_notes.py --list-file update_notelist.txt            # ドライラン
    python sync_updated_notes.py --execute --list-file update_notelist.txt  # 本番実行
デスクトップ等から実行する場合は「個別変更リスト.bat」を使用する。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import auto_sync_blogs as core

SCRIPT_DIR = Path(__file__).resolve().parent
PROGRESS_PATH = SCRIPT_DIR / "sync_updated_notes_progress.json"


def load_checked_keys() -> set[str]:
    if PROGRESS_PATH.exists():
        return set(json.loads(PROGRESS_PATH.read_text(encoding="utf-8")))
    return set()


def save_checked_keys(keys: set[str]) -> None:
    PROGRESS_PATH.write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=2), encoding="utf-8")


NOTE_KEY_PATTERN = re.compile(r"(?:/n/|^)(n[0-9a-zA-Z]+)(?:[/?#]|$)")


def load_list_file_keys(path: Path) -> list[str]:
    """リストファイルからnote記事キーを順序を保ったまま重複なく取り出す。"""
    keys: list[str] = []
    # メモ帳で保存されたBOM付きUTF-8でも先頭行のキーが崩れないよう utf-8-sig で読む
    for lineno, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = NOTE_KEY_PATTERN.search(line)
        if not m:
            print(f"  [警告] {path.name} {lineno}行目: note記事のURLとして解釈できないためスキップします: {line}")
            continue
        if m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def run_list_file_mode(wp, state: dict, creds: dict, list_path: Path, execute: bool, link_maps) -> None:
    keys = load_list_file_keys(list_path)
    print(f"リストファイル {list_path.name} から{len(keys)}件の記事を読み込みました")
    if not keys:
        print("対象記事はありませんでした（リストファイルにURLを1行に1つ記入してください）。")
        return

    articles_by_key = {a["key"]: a for a in core.fetch_note_article_list()}
    targets = []
    log_rows: list[dict] = []
    for key in keys:
        if key in articles_by_key:
            targets.append(articles_by_key[key])
            continue
        # 一覧APIに出ない公開記事（note側で「クリエイターページに表示しない」設定）は
        # 単体記事APIから直接取得する
        article = core.fetch_note_article_by_key(key)
        if article:
            print(f"  [情報] {key} は公開一覧に無いため、記事URLから直接取得しました")
            targets.append(article)
        else:
            # 下書き・削除済み・別アカウントの記事・URLの誤記は本文も価格情報も取れないため同期できない
            print(f"  [警告] {key} はnote.comの公開済み記事に見つからないためスキップします")
            log_rows.append(core.log_row("list_sync", None, key, "", "not_found_on_note"))

    # 先にWP側の存在有無で振り分ける。sync_new_note_posts で作成した直後の記事を
    # sync_note_updates に渡すと、同じ記事を二重に書き込んでしまうため。
    existing = [a for a in targets if wp.find_post_by_slug(a["key"]) is not None]
    missing = [a for a in targets if a not in existing]
    print(f"WordPressに既存: {len(existing)}件（強制上書き更新） / 未投稿: {len(missing)}件（新規投稿）")

    if missing:
        log_rows += core.sync_new_note_posts(wp, missing, state, execute, None, link_maps)
        core.save_state(state)
    if existing:
        # force_keys に全件を渡すことで、公開日ウィンドウと本文ハッシュ判定の両方を無視させる
        log_rows += core.sync_note_updates(
            wp, existing, state, execute, None, link_maps,
            force_keys={a["key"] for a in existing},
        )
        core.save_state(state)

    core.relink_touched_posts(wp, log_rows, execute)
    if execute and any(r["result"] == "success" for r in log_rows):
        core.regenerate_tag_index_page(wp, creds)
    report_result(log_rows, execute)


def report_result(log_rows: list[dict], execute: bool) -> None:
    core.append_log(log_rows)
    # sync_note_updates() は実際に更新した記事を result="success"、
    # ドライランで更新見込みと判定した記事を result="dry_run_would_update"
    # として log_rows に積む（変更なしの記事はそもそもログに積まれない）。
    # --list-file モードでは sync_new_note_posts の新規作成分
    # （task="new_note"、ドライラン時は "dry_run_would_create"）も混ざる。
    updated = [r for r in log_rows if r["result"] in ("success", "dry_run_would_update", "dry_run_would_create")]

    print("\n" + "=" * 60)
    print("差分検知・更新の結果")
    print("=" * 60)
    if updated:
        print(f"反映{'された' if execute else '見込みの'}記事: {len(updated)}件")
        for r in updated:
            kind = "新規" if r["task"] == "new_note" else "更新"
            print(f"  - [{kind}] {r['title']}")
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
    parser.add_argument(
        "--recent-days", type=int, default=None,
        help="指定した場合、公開からこの日数以内の記事のみを対象にする軽量モード（省略時は全note記事が対象）",
    )
    parser.add_argument("--limit", type=int, default=None, help="対象件数の上限（試験実行用）")
    parser.add_argument(
        "--batch-size", type=int, default=None,
        help="全件モード（--recent-days未指定時）のみ有効。この回で本文を確認する最大件数。"
             "指定件数を確認したらプロセスを正常終了する（メモリを完全に解放するため、"
             "バッチファイル側で本プロセスごと繰り返し起動する運用を想定）。"
             "終了コード: 0=今回のサイクルで全件確認完了、2=まだ未確認の記事が残っている。",
    )
    parser.add_argument(
        "--list-file", type=Path, default=None,
        help="note記事のURLを1行に1つ記載したテキストファイル。指定すると差分判定をスキップし、"
             "記載された記事だけをWordPressへ強制同期する（既存は上書き更新、未投稿は新規投稿）。",
    )
    args = parser.parse_args()

    creds = core.load_credentials()
    wp = core.WP(creds["site_url"], creds["username"], creds["application_password"])
    state = core.load_state()

    mode = "本番実行（--execute）" if args.execute else "ドライラン（変更なし）"

    if args.list_file:
        list_path = args.list_file
        if not list_path.is_absolute() and not list_path.exists():
            list_path = SCRIPT_DIR / list_path
        if not list_path.exists():
            print(f"[エラー] リストファイルが見つかりません: {list_path}")
            sys.exit(1)
        print("=" * 60)
        print(f"note記事 個別強制同期（リストファイル指定）  実行モード: {mode}")
        print("=" * 60)
        link_maps = core.fetch_link_maps(wp)
        print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")
        run_list_file_mode(wp, state, creds, list_path, args.execute, link_maps)
        return

    is_full_scan = args.recent_days is None
    scope = "全note記事（約360件）" if is_full_scan else f"直近{args.recent_days}日以内の記事"
    print("=" * 60)
    print(f"note過去記事 差分自動検知・更新  実行モード: {mode}")
    print(f"対象範囲: {scope}")
    print("=" * 60)

    link_maps = core.fetch_link_maps(wp)
    print(f"内部リンク変換用の対応表を取得しました: note {len(link_maps[0])}件 / exblog {len(link_maps[1])}件")

    articles = core.fetch_note_article_list()
    print(f"note公開済み記事一覧を取得しました: {len(articles)}件")

    if not is_full_scan:
        # 軽量モード：対象件数が少ないため1回のプロセスで完走できる。
        log_rows = core.sync_note_updates(
            wp, articles, state, args.execute, args.limit, link_maps,
            window_days=args.recent_days,
        )
        core.save_state(state)
        report_result(log_rows, args.execute)
        return

    # 全件モード（デフォルト）：360件規模のためバッチ分割に対応する。
    checked = load_checked_keys()
    pending = [a for a in articles if a["key"] not in checked]
    print(f"今回のサイクルで未確認: {len(pending)}件（{len(checked)}件は確認済み）")

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
