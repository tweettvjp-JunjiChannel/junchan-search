"""【2026-10-13】note-style-engagement.php のインラインJSを抽出し、模擬Codocウィジェットの各状態で
サブスク期限切れ表示・文言変更が意図通りに動くかをPlaywrightで検証する（本番非接触）。"""
import json
import os
import re
import sys
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "note-style-engagement.php")
src = open(SRC, encoding="utf-8").read()
css = re.search(r'<style id="nseb-frontend-css">(.*?)</style>', src, re.S).group(1)
js = re.search(r'<script id="nseb-frontend-js">(.*?)</script>', src, re.S).group(1)
js = js.replace("<?php echo wp_json_encode($rest_root); ?>", '"http://test.local/wp-json/engage/v1"')
js = js.replace("<?php echo wp_json_encode(self::CONTENT_SUBSCRIPTION_DOM_ID); ?>", '"codoc-subscription-oEplngWcvQ"')
assert "<?php" not in js, "未置換のPHPタグがあります"

PAID = {"purchased_count": 0, "price_label": "¥300", "is_free": False, "view_count": 5,
        "like_count": 0, "view_today": 0, "view_week": 0, "view_month": 0}
PAID_100 = dict(PAID, price=100, price_label="100円")
FREE = dict(PAID, price=0, price_label="無料", is_free=True, purchased_count=None)
FREE_IDS = {"900"}
CHEAP_IDS = {"500"}


def codoc_block(logged_in, locked, offers_sub):
    user = '<div class="codoc-user"><strong>テスト読者</strong></div>' if logged_in else ""
    buy_style = "" if locked else ' style="display: none;"'
    sub = ('<ul class="codoc-subscription-list"><li class="codoc-subscription-group">'
           '<div class="codoc-subscription-info"><div class="codoc-subscription-title">月額読み放題</div></div></li></ul>'
           if offers_sub else "")
    body_after = "" if locked else "続きの本文です。" * 10
    pad = "x" * 600
    return (f'<div class="wp-block-codoc-codoc-block"><div class="codoc-entries">{user}'
            f'<div class="codoc-buy-wrap"{buy_style}><div class="codoc-buy">記事を購入</div>{sub}</div>'
            f'<div class="codoc-entry-body-after">{body_after}</div><span hidden>{pad}</span>'
            f'<div class="codoc-copyright">powered by codoc</div></div></div>')


def detail_page(post_id, block):
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head>
<body class="single postid-{post_id}"><h1 class="entry-title">テスト記事</h1>
<div class="nseb-status-bar" data-post-id="{post_id}" data-paid="1">
  <span class="nseb-stat nseb-stat-purchased nseb-skeleton" hidden style="display:none">…</span>
  <span class="nseb-stat nseb-price nseb-skeleton">…</span>
  <span class="nseb-stat nseb-stat-views"><span class="nseb-view-count nseb-skeleton">…</span></span>
  <button type="button" class="nseb-stat nseb-like-btn" data-post-id="{post_id}"><span class="nseb-heart">♡</span><span class="nseb-like-count">…</span></button>
</div>
<div class="entry-content"><p>無料部分</p>{block}</div>
<script>{js}</script></body></html>"""


def library_page(ids):
    cards = "".join(f'<article id="post-{i}"><div class="entry-card-meta"></div></article>' for i in ids)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{css}</style></head><body class="home">
<div id="nseb-my-library-tabs"><a href="#" data-tab="purchased" class="nseb-library-tab is-active">📖 サブスク既読記事</a>
<a href="#" data-tab="liked" class="nseb-library-tab">♥ スキした記事</a>
<a href="#" data-tab="both" class="nseb-library-tab">🌟 サブスク既読記事＆スキ</a></div>{cards}
<script>{js}</script></body></html>"""


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("✅ 合格" if cond else "❌ 不合格"), name, detail)


def run(ctx, html, storage):
    page = ctx.new_page()
    ids_seen = {}

    def handler(route):
        url = route.request.url
        if "/card-data" in url:
            ids = re.search(r"ids=([0-9,]+)", url).group(1).split(",")
            route.fulfill(status=200, content_type="application/json", body=json.dumps({i: (FREE if i in FREE_IDS else PAID_100 if i in CHEAP_IDS else PAID) for i in ids}))
        elif "/view/" in url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(PAID))
        elif url.startswith("http://test.local/page"):
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        else:
            route.fulfill(status=200, body="[]", content_type="application/json")

    page.route("http://test.local/**", handler)
    page.goto("http://test.local/blank")
    page.evaluate("s => { localStorage.clear(); for (const k in s) localStorage.setItem(k, s[k]); }", storage)
    page.goto("http://test.local/page")
    page.wait_for_timeout(800)
    return page


def ls(page, key):
    return page.evaluate("k => localStorage.getItem(k)", key)


with sync_playwright() as pw:
    browser = pw.chromium.launch()
    ctx = browser.new_context()

    # A: 過去にサブスク利用の形跡あり＋ログイン済み＋プラン対象記事でロック中 → 期限切れ
    p = run(ctx, detail_page(101, codoc_block(True, True, True)),
            {"codoc_active_subscriber": "1", "nseb_purchased_posts": "[101,102]"})
    stat = p.inner_text(".nseb-stat-purchased")
    check("A1 期限切れフラグが立つ", ls(p, "codoc_subscription_expired") == "1")
    check("A2 読み放題推定フラグが取り消される", ls(p, "codoc_active_subscriber") is None)
    check("A3 詳細ページに期限切れ案内枠が出る", p.locator(".nseb-sub-expired-notice").count() == 1,
          p.locator(".nseb-sub-expired-notice").inner_text() if p.locator(".nseb-sub-expired-notice").count() else "")
    check("A4 ステータスバーが『サブスク期限切れ（要更新）』", "サブスク期限切れ" in stat, stat)
    check("A5 マイ本棚の既読記録は消えない", 101 in json.loads(ls(p, "nseb_purchased_posts")))
    check("A6 案内枠が統合案内枠より上にある",
          p.evaluate("() => { const n=document.querySelector('.nseb-sub-expired-notice'), g=document.querySelector('.nseb-purchase-guide'); return !!(n&&g&&(n.compareDocumentPosition(g)&Node.DOCUMENT_POSITION_FOLLOWING)); }"))
    check("A7 統合案内枠の復元ボタン文言", "購入済み・サブスクを復元" in p.inner_text(".nseb-purchase-guide-btn"))
    p.close()

    # B: 期限切れ後にサブスク更新してアンロック → 期限切れ解除・既読/読み放題表示
    p = run(ctx, detail_page(101, codoc_block(True, False, True)),
            {"codoc_subscription_expired": "1", "nseb_purchased_posts": "[101,102]"})
    stat = p.inner_text(".nseb-stat-purchased")
    check("B1 アンロックで期限切れフラグ解除", ls(p, "codoc_subscription_expired") is None)
    check("B2 期限切れ案内枠は出ない", p.locator(".nseb-sub-expired-notice").count() == 0)
    check("B3 ステータスバーが既読/読み放題", ("読み放題" in stat) or ("既読" in stat), stat)
    check("B4 『購入済み』表記が残っていない", "購入済み" not in stat, stat)
    p.close()

    # C: ログイン済みだが一度もサブスク利用の形跡なし（未加入者） → 期限切れ扱いしない
    p = run(ctx, detail_page(201, codoc_block(True, True, True)), {})
    check("C1 未加入者は期限切れにならない", ls(p, "codoc_subscription_expired") is None)
    check("C2 案内枠も出ない", p.locator(".nseb-sub-expired-notice").count() == 0)
    p.close()

    # D: 形跡あり・ログイン済みだが購読プラン未紐付け記事でロック → 期限切れ扱いしない
    p = run(ctx, detail_page(301, codoc_block(True, True, False)),
            {"codoc_active_subscriber": "1", "nseb_purchased_posts": "[101,102,301]"})
    check("D1 プラン未紐付け記事では期限切れにならない", ls(p, "codoc_subscription_expired") is None)
    check("D2 従来どおり誤った既読フラグは取り消される", 301 not in json.loads(ls(p, "nseb_purchased_posts")))
    p.close()

    # E: 未ログイン（.codoc-user無し）でロック → 期限切れ判定しない（別端末・認証切れの可能性）
    p = run(ctx, detail_page(101, codoc_block(False, True, True)),
            {"codoc_active_subscriber": "1", "nseb_purchased_posts": "[101,102]"})
    check("E1 未ログインでは期限切れにならない", ls(p, "codoc_subscription_expired") is None)
    p.close()

    # F: 旧不具合の回帰確認：ログイン済み(.codoc-user)だけでアンロック扱いにしない
    p = run(ctx, detail_page(401, codoc_block(True, True, False)), {})
    check("F1 .codoc-userのみでは既読フラグを立てない", ls(p, "nseb_purchased_posts") in (None, "[]"))
    p.close()

    # G: マイ本棚＋一覧カード（期限切れ状態）
    p = run(ctx, library_page([101, 102, 500, 900]),
            {"codoc_subscription_expired": "1", "nseb_purchased_posts": "[101,102]"})
    p.wait_for_selector("#post-101 .nseb-card-purchased")
    check("G1 マイ本棚に期限切れ案内", p.locator(".nseb-library-expired-notice").count() == 1)
    b101 = p.inner_text("#post-101 .nseb-card-purchased")
    check("G2 既読記事カードが『要サブスク更新』", "要サブスク更新" in b101, b101)
    b500 = p.inner_text("#post-500 .nseb-card-purchased") if p.locator("#post-500 .nseb-card-purchased").count() else ""
    check("G3 未読の100円記事(500)にも『要サブスク更新』", "要サブスク更新" in b500, b500)
    check("G4 無料記事(900)には期限切れバッジを出さない",
          p.locator("#post-900 .is-expired").count() == 0)
    p.close()

    # H: マイ本棚＋一覧カード（通常の既読、期限切れでない）
    p = run(ctx, library_page([101]), {"nseb_purchased_posts": "[101]"})
    p.wait_for_selector("#post-101 .nseb-card-purchased")
    b = p.inner_text("#post-101 .nseb-card-purchased")
    check("H1 既読カードが『✅既読』", "既読" in b and "購入済み" not in b, b)
    check("H2 期限切れでなければ案内なし", p.locator(".nseb-library-expired-notice").count() == 0)
    p.close()

    browser.close()

ng = [n for n, ok in results if not ok]
print(f"\n合計 {len(results)} 件中 {len(results) - len(ng)} 件合格")
sys.exit(1 if ng else 0)
