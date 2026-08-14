"""
E2E回帰テスト: Codocウィジェットの「ロック解除」DOM状態を正しく検知して
購入済みフラグ（LocalStorage の nseb_purchased_posts）を立てられることを検証する。

【背景】2026-08-14: test_e2e_purchase_highlight.py で赤色ハイライト自体の
描画ロジックは検証済みだったが、実際に記事を購入した本物の閲覧者
（Codocのオーディエンス/売上ページに記録されている実在の購入者、
ニックネーム「忍者トゥルーサー」）のブラウザで確認したところ、詳細・一覧
どちらのページでも🛒バッジが赤くならないという報告を受けた。

実機HTML（購入者本人のブラウザから取得）を調査した結果、当初の検知ロジックの
前提が誤っていたことが判明した：
  - 誤：「ロック解除時は購入ボタン(.codoc-buy-wrap)がDOMから消える」
  - 正：ロック解除時も .codoc-buy-wrap 要素自体は残ったまま、インライン
    style="display: none" で非表示にされるだけ。要素の「存在」で判定
    していたため、実際の購入者でも常に「購入ボタンあり＝未購入」と
    誤判定していた（＝実際の購入者が一度も赤くならない不具合の直接原因）。

あわせて実機HTMLから、ロック解除時にのみ現れる2つの追加シグナルも判明した：
  - `.codoc-user`（購入者のニックネームを表示する要素。例:
    `<div class="codoc-user"><strong>忍者トゥルーサー</strong></div>`）
  - `.codoc-entry-body-after`（本文の続きを差し込む要素。ロック中は同じ
    要素が存在するが中身は空。ロック解除時は実際の本文がここに入る）

修正後は、(1) .codoc-buy-wrap が非表示（style/computed styleのどちらでも
display:noneと判定できる場合、または要素自体が存在しない場合）、
(2) .codoc-user が存在する、(3) .codoc-entry-body-after に空でない
本文が入っている、のいずれか1つでも満たせば購入済みと判定するよう
checkCodocPurchaseState() を修正した（note-style-engagement.php v2.4.1）。

本物の購入者アカウントでログインし直してテストすることはできないため、
このテストは実際の詳細ページを開いて本物の（ロック中の）Codocウィジェットが
描画・監視開始されるのを待った後、購入者から提供された実機HTMLそのままの
構造でウィジェットのコンテナDOMを書き換え、既にアタッチ済みの
MutationObserver（checkCodocPurchaseState内）がこの変化を正しく検知して
購入済みフラグを立てることを確認する。

シナリオ:
    1. 実際の記事詳細ページを匿名で開き、本物のCodocウィジェット（ロック状態）
       が描画されるのを待つ（この時点でLocalStorageに購入済みフラグが
       立っていないことも確認＝誤検知が無いことの再確認）。
    2. ウィジェットのコンテナDOMを、実際の購入者ブラウザで確認された
       ロック解除状態のHTML構造に書き換える。
    3. 購入済みフラグが立つことを確認する。

実行方法:
    python test_e2e_purchase_unlock_detection.py
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

DETAIL_URL_BASE = "https://junchan-world.com/nf66f2dc0c727/"
POST_ID = 57150

# 実際の購入者（忍者トゥルーサー）のブラウザから提供されたロック解除状態の
# DOM構造をそのまま模したもの。
UNLOCKED_HTML = """
<div class="codoc-entry">
  <div class="codoc-buy-wrap" style="display: none;"><div class="codoc-buy"><a href="javascript:void(0);" class="codoc-btn"><span>記事を購入</span></a></div></div>
  <div id="codoc-entry-body-after-e1BTEzpK9w" class="codoc-entry-body-after"><p>（アンロックされた本文のダミーテキスト）</p></div>
  <div class="codoc-user"><strong>忍者トゥルーサー</strong></div>
</div>
<div class="codoc-copyright codoc-copyright-middle">powered by codoc</div>
"""


def get_purchased_flags(page) -> list:
    return page.evaluate(
        "() => { try { return JSON.parse(window.localStorage.getItem('nseb_purchased_posts') || '[]'); } "
        "catch (e) { return null; } }"
    )


def main() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("=" * 60)
        print("ステップ1: 実際の記事詳細ページを匿名で開く（ロック状態の確認）")
        print("=" * 60)
        url = DETAIL_URL_BASE + "?_e2e=" + str(int(time.time() * 1000))
        page.goto(url, wait_until="networkidle", timeout=45000)
        page.wait_for_selector(".wp-block-codoc-codoc-block .codoc-copyright", timeout=15000)
        page.wait_for_timeout(500)

        before = get_purchased_flags(page)
        print(f"ロック状態でのLocalStorageフラグ: {before}")
        assert before == [], (
            f"[FAIL] 前提条件エラー: 匿名の未購入訪問者なのに購入済みフラグが立っています: {before}"
            "（フォールスポジティブの再発）"
        )
        print("[OK] ロック状態では購入済みフラグが立っていない（誤検知なし）")

        print("\n" + "=" * 60)
        print("ステップ2: ウィジェットのDOMを実際の購入者のロック解除状態に書き換える")
        print("=" * 60)
        page.evaluate(
            "(html) => { document.querySelector('.wp-block-codoc-codoc-block').innerHTML = html; }",
            UNLOCKED_HTML,
        )
        page.wait_for_timeout(1000)

        print("\n" + "=" * 60)
        print("ステップ3: 購入済みフラグが立ったことを確認する")
        print("=" * 60)
        after = get_purchased_flags(page)
        print(f"ロック解除後のLocalStorageフラグ: {after}")
        assert after == [POST_ID], (
            f"[FAIL] ロック解除状態のDOM（購入ボタン非表示+codoc-user+codoc-entry-body-after）"
            f"を検知できず、購入済みフラグが立っていません: {after}"
        )
        print(f"[OK] ロック解除状態を正しく検知し、post_id={POST_ID} の購入済みフラグが立った")

        browser.close()

    print("\n" + "=" * 60)
    print("E2E TEST PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
