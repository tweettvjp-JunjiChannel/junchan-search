"""
Codocサーバー側の「購読プラン紐付け」バックフィルスクリプト。

【背景】2026-08-25、実機（スマホ・JAL123便の記事 n743303f7d21b 等）で、
Codocにサブスク加入済みの状態でもペイウォールが解除されない不具合が報告された。
調査の結果、WordPress投稿内のCodocブロックJSON属性（"subscriptions":{"4133":true}）
は、Codocサーバー側の実際の紐付けレコードとは別物であり、REST API経由で
作成されたWordPress記事では、Codocダッシュボード側の個別記事編集ページ
（https://codoc.jp/me/entries/{entry_code}/edit）にある「購読プラン販売」の
チェックボックス（name="subscriptions[]" value="4133"）が一度もONにならないまま
放置されていたことが判明した（実機で5記事サンプル調査、全てFalseを確認）。
この状態では、Codocの公開API（body.json）の "subscriptions" 配列が空のままで、
cms.jsウィジェットがこの記事を「どの購読プランにも属さない単体販売記事」として
扱い、サブスク会員であってもペイウォールを解除しない。

このスクリプトは、WordPress側でcodoc_entry_codeを持つ全ての有料記事について、
Codocダッシュボードの個別記事編集ページを開き、このチェックボックスがOFFの
場合のみONにして保存する（既にONの記事はスキップ＝再実行しても安全な冪等処理）。

【注意】Codoc公式のダッシュボードには「WordPressで作成された記事の編集は
推奨されません」という警告が出るが、実際に本文欄（body_free/body_paywalled）
には正しい記事本文が既に反映されていることを確認済み（Codoc側が別経路で
本文を同期している）。チェックボックスをONにして保存するだけで、本文自体を
上書き・改変するものではない。

チェックポイント：処理済みのentry_codeと結果を1件ごとに
codoc_subscription_backfill_progress.json に追記保存し、中断しても
再実行時に未処理分のみ再開できる。

実行方法:
    python backfill_codoc_subscription_linkage.py              # ドライラン（調査のみ）
    python backfill_codoc_subscription_linkage.py --execute    # 本番実行
    python backfill_codoc_subscription_linkage.py --execute --limit 5   # 試験実行

【再発防止】この不具合はWordPress記事のREST API新規作成時に必ず起きる
（実機検証済み：REST再保存を挟んでもこのチェックボックスは変化しない。
「無変更のまま再保存するとcodocと同期される」という過去の知見は価格等
別のフィールドの話であり、subscriptionsには当てはまらない）。そのため
このファイルの process_entry() を auto_sync_blogs.py の新規note記事投稿
処理（sync_new_note_posts）からも import して、記事投稿と同じタイミングで
毎回自動的にこの紐付けを行うようにしてある。このスクリプト単体は、それでも
取りこぼした記事や過去記事向けの手動バックフィル用として引き続き使う。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import requests
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = SCRIPT_DIR / "wp_credentials.json"
PROGRESS_PATH = SCRIPT_DIR / "codoc_subscription_backfill_progress.json"
LOG_PATH = SCRIPT_DIR / "codoc_subscription_backfill_log.csv"
CHROME_USER_DATA_DIR = SCRIPT_DIR / "chrome_user_data"

PLAN_VALUE = "4133"


def load_credentials() -> dict:
    data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    return data


def fetch_paid_entry_codes(site_url: str, auth: tuple[str, str]) -> list[dict]:
    s = requests.Session()
    s.auth = auth
    posts = []
    page = 1
    while True:
        r = s.get(
            f"{site_url}/wp-json/wp/v2/posts",
            params={
                "per_page": 100, "page": page, "orderby": "id", "order": "asc",
                "_fields": "id,link,meta",
            },
            timeout=30,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        total_pages = int(r.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1

    paid = []
    for p in posts:
        code = p.get("meta", {}).get("codoc_entry_code")
        if code:
            paid.append({"id": p["id"], "link": p["link"], "entry_code": code})
    return paid


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {}


def save_progress_entry(progress: dict, entry_code: str, result: dict) -> None:
    progress[entry_code] = result
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")


def append_log(rows: list[dict]) -> None:
    if not rows:
        return
    is_new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "entry_code", "post_id", "link", "status", "detail"])
        if is_new:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def ensure_logged_in(page) -> bool:
    """Codocのセッションが切れてログイン画面へリダイレクトされていた場合、
    Chromeの永続プロファイルに保存済みの自動入力済み認証情報でログインし直す。
    2026-08-30、新規note記事8件中5件で発生した checkbox_not_found の真因が
    「UI変更やタイミングではなくセッション切れでログイン画面にいた」ことだった
    ため追加。新しい資格情報を入力するのではなく、既にブラウザが自動入力した
    フィールドの値を使ってログインボタンを押すだけなので機密情報を扱わない。

    2026-09-13追記：Codoc側に二段階認証（メール宛6桁コード）が新たに導入され、
    ログインボタン押下後に /two_factor/auth へ遷移するケースが発生することを
    確認した。旧実装は「URLに/loginを含まない＝ログイン成功」という判定だった
    ため、2FA待ちpage（/two_factor/authもURLに/loginを含まない）を誤って
    「ログイン成功」と誤判定し、その後のcheckbox探索が必ずcheckbox_not_found
    になるバグがあった。2FAコードの自動入力はできない（メール受信者本人しか
    読めない）ため、2FA待ちを検知した場合は明示的にlogin_failedとして扱い、
    人間が事前に一度だけ手動でこのブラウザプロファイルを認証・信頼済み
    （「このデバイスを30日間信頼する」）にしておく運用を前提とする。
    """
    if "/two_factor" in page.url:
        return False
    if "/login" not in page.url:
        return True
    login_btn = page.locator('button:has-text("ログイン")').first
    if login_btn.count() == 0:
        return False
    login_btn.click()
    page.wait_for_load_state("load", timeout=30000)
    page.wait_for_timeout(1200)
    if "/two_factor" in page.url:
        return False
    return "/login" not in page.url


def process_entry(page, entry_code: str, execute: bool) -> dict:
    page.goto(f"https://codoc.jp/me/entries/{entry_code}/edit", wait_until="load", timeout=30000)
    page.wait_for_timeout(900)

    if not ensure_logged_in(page):
        return {"status": "login_failed"}

    sub_cb = page.locator('input[name="subscriptions[]"][value="' + PLAN_VALUE + '"]')
    if sub_cb.count() == 0:
        return {"status": "checkbox_not_found"}

    already_checked = sub_cb.is_checked()
    if already_checked:
        return {"status": "already_linked"}

    if not execute:
        return {"status": "dry_run_would_link"}

    continue_cb = page.locator('input[name="continue_editing"]')
    if continue_cb.count() > 0 and not continue_cb.is_checked():
        continue_cb.check()
        page.wait_for_timeout(400)

    sub_cb.check()
    page.wait_for_timeout(300)

    save_btn = page.locator('button:has-text("更新"), input[value="更新"]').first
    if save_btn.count() == 0:
        return {"status": "save_button_not_found"}
    save_btn.click()
    page.wait_for_timeout(1800)

    # verify persistence with a fresh reload
    page.goto(f"https://codoc.jp/me/entries/{entry_code}/edit", wait_until="load", timeout=30000)
    page.wait_for_timeout(900)
    verify_cb = page.locator('input[name="subscriptions[]"][value="' + PLAN_VALUE + '"]')
    if verify_cb.count() > 0 and verify_cb.is_checked():
        return {"status": "linked_and_verified"}
    return {"status": "link_failed_verification"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--recheck-all",
        action="store_true",
        help=(
            "進捗ファイルのキャッシュ（already_linked/linked_and_verified）を無視し、"
            "全件をCodoc側で再確認する。2026-09-01判明: WordPress投稿のcontentを"
            "REST APIで更新すると、Codoc側の購読プラン紐付けチェックボックスが"
            "サイレントに解除される副作用があることが確認された（内部リンク一括"
            "変換等でcontentを更新した記事で、紐付け済みだったはずの記事が軒並み"
            "解除されていた）。進捗ファイルは「一度linkedと確認した」過去の事実を"
            "記録しているに過ぎず、その後のcontent更新で覆っている可能性があるため、"
            "content更新系スクリプトを実行した後は必ずこのフラグ付きで再監査すること。"
        ),
    )
    args = parser.parse_args()

    creds = load_credentials()
    paid_posts = fetch_paid_entry_codes(creds["site_url"].rstrip("/"), (creds["username"], creds["application_password"]))
    print(f"WordPress側の有料記事（codoc_entry_code保持）: {len(paid_posts)}件")

    progress = load_progress()
    if args.recheck_all:
        pending = paid_posts
        print("--recheck-all: 進捗ファイルのキャッシュを無視し、全件を再確認します")
    else:
        pending = [p for p in paid_posts if p["entry_code"] not in progress or progress[p["entry_code"]].get("status") not in ("already_linked", "linked_and_verified")]
        print(f"未処理・要再確認: {len(pending)}件（進捗ファイルから{len(paid_posts) - len(pending)}件をスキップ）")

    if args.limit:
        pending = pending[: args.limit]

    mode = "本番実行" if args.execute else "ドライラン"
    print(f"実行モード: {mode}")

    log_rows = []
    counts: dict[str, int] = {}

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(CHROME_USER_DATA_DIR), channel="chrome", headless=True, viewport={"width": 1400, "height": 1200}
        )
        page = context.pages[0] if context.pages else context.new_page()

        for i, p in enumerate(pending, start=1):
            code = p["entry_code"]
            try:
                result = process_entry(page, code, args.execute)
            except Exception as e:
                result = {"status": "error", "detail": str(e)[:200]}

            status = result["status"]
            counts[status] = counts.get(status, 0) + 1
            save_progress_entry(progress, code, result)
            log_rows.append({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_code": code, "post_id": p["id"], "link": p["link"],
                "status": status, "detail": result.get("detail", ""),
            })
            if i % 10 == 0 or i == len(pending):
                print(f"  [{i}/{len(pending)}] id={p['id']} entry_code={code} -> {status}")
            time.sleep(0.2)

        context.close()

    append_log(log_rows)
    print()
    print("=" * 60)
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}件")
    print(f"ログを保存しました: {LOG_PATH}")
    if not args.execute:
        print("これはドライランです。実際には何も変更されていません。--execute を付けて実行すると反映されます。")
    print("=" * 60)


if __name__ == "__main__":
    main()
