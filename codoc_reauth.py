"""
Codoc二段階認証（メール宛6桁コード）の手動再認証用スクリプト。

【背景】Codoc関連の自動化（auto_sync_blogs.py の購読プラン紐付け、
backfill_codoc_subscription_linkage.py 等）は、永続Chromeプロファイル
（chrome_user_data）に保存されたCodocのログイン状態を使う。2FAコードは
メール受信者本人しか読めず自動入力できないため、30日ごとに一度、人間が
このプロファイルで手動ログインし「このデバイスを30日間信頼する」を
有効にしておく必要がある（CLAUDE.md 18項参照）。

このスクリプトは、自動化と同じプロファイルで画面付きのChromeを開くだけで、
ID・パスワード・認証コードには一切触れない（入力はすべて人間がブラウザ上で行う）。
コードはログイン操作のたびに再送され直前のものが無効になるため、
メールに届いた最新のコードを入力すること。

【注意】他の同期スクリプト（週次実行.bat等）が動いている間は同じプロファイルを
同時に開けないため、それらが終わってから実行すること。

実行方法:
    python codoc_reauth.py      （または「Codoc再認証.bat」をダブルクリック）
"""

from __future__ import annotations

import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.sync_api import sync_playwright

import auto_sync_blogs as core

CHECK_URL = "https://codoc.jp/me/entries"


def is_logged_in(page) -> bool | None:
    """ログイン済みならTrue、未ログインならFalse、通信エラー等で判定できなければNone。"""
    try:
        # wait_until="load" は画像・外部スクリプト等の読み込み完了まで待つため、
        # 実機で30秒タイムアウトが発生した。ログイン判定に必要なのはリダイレクト後の
        # URLだけなので、HTML本体が届いた時点（domcontentloaded）で十分。
        page.goto(CHECK_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)
    except Exception as e:
        print(f"\n[通信エラー] Codocのページを開けませんでした: {str(e).splitlines()[0]}")
        return None
    return "/login" not in page.url and "/two_factor" not in page.url


def main() -> None:
    with sync_playwright() as pw:
        context, page = core.get_note_browser_page(pw, headless=False)
        try:
            already = is_logged_in(page)
            print("=" * 60)
            print("Codoc 二段階認証の再設定")
            print("=" * 60)
            if already:
                print("現在はCodocにログイン済みです。")
                print("30日間の信頼期間を更新し直す場合は、開いたブラウザ右上のメニューから")
                print("一度ログアウトし、もう一度ログインしてください。")
            elif already is None:
                print("ログイン状態を確認できませんでした（通信エラー）。")
                print(f"開いたブラウザで {CHECK_URL} を開き直し、ログインしてください。")
            else:
                print("Codocのログインが切れています。開いたブラウザでログインしてください。")
            print()
            print("手順:")
            print("  1. メールアドレス・パスワードでログイン")
            print("  2. メールに届いた6桁の認証コードを入力")
            print("     （必ず「このデバイスを30日間信頼する」にチェックを入れる）")
            print("  3. 記事一覧（ダッシュボード）が表示されたら、この画面に戻って Enter を押す")
            print()
            input("ログインが終わったら Enter を押してください...")

            # 通信エラーやログイン未完了でも落とさず、Enterで何度でも確認し直せるようにする
            while True:
                result = is_logged_in(page)
                if result:
                    print("\n[OK] Codocへのログインを確認しました。以後30日間は自動化がそのまま動きます。")
                    return
                if result is False:
                    print(f"\n[未完了] まだログインできていません（現在のURL: {page.url}）。")
                    print("ブラウザでログインと認証コード入力を済ませてください。")
                answer = input("Enter で再確認 / q + Enter で終了: ").strip().lower()
                if answer == "q":
                    print("確認を中断しました。もう一度このスクリプトを実行してやり直してください。")
                    sys.exit(1)
        finally:
            context.close()


if __name__ == "__main__":
    main()
